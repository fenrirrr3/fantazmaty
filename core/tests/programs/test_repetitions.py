"""Analiza powtórzeń: kolorowanie odmian słów, dodatkowe kontrole, palety kolorów, formularz i strona programu."""
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX

from core.odkurzacz_forms import RepetitionsForm
from core.program_test_support import run_program
from core.services.document_converter import convert_document
from core.services.document_repetitions import color_document
from fantazmaty.testing import create_member

from .support import (
    REPETITION_FIELDS, REPETITION_PARAMETERS, IsolatedProgramDirsMixin, downloaded_document, footnote_text,
    saved, whole_document,
)

ONLY_REPETITIONS = {'duplicates': False, 'long_sentences': False, 'long_paragraphs': False, 'empty_pairs': False}


def payload(document):
    return saved(document).getvalue()


def formatted_repetition():
    """„Koty koty.” – pogrubione pierwsze słowo, kursywa w drugim przebiegu."""
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run('Koty').bold = True
    paragraph.add_run(' koty.').italic = True
    return payload(document)


def font_colors(paragraph):
    return [run.font.color.rgb for run in paragraph.runs if run.font.color.rgb]


class RepeatAnalysisTests(SimpleTestCase):
    def test_real_polish_model_flexion_and_run_preservation(self):
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.add_run('Kot').bold = True
        paragraph.add_run(' spaceruje obok ')
        paragraph.add_run('kota').italic = True
        paragraph.add_run('.  Bez zmian!')
        document.add_paragraph('')
        marked = Document(color_document(BytesIO(payload(document)), min_word_length=3, analysis_options={
            'long_sentences': False, 'long_paragraphs': False, 'empty_pairs': False}))
        self.assertEqual([p.text for p in marked.paragraphs], [p.text for p in document.paragraphs])
        self.assertEqual(marked.paragraphs[0].alignment, WD_ALIGN_PARAGRAPH.CENTER)
        runs = marked.paragraphs[0].runs
        self.assertEqual(runs[0].font.color.rgb, runs[2].font.color.rgb)
        self.assertIsNotNone(runs[0].font.color.rgb)
        self.assertTrue(runs[0].bold)
        self.assertTrue(runs[2].italic)

    def test_cross_run_word_and_ignored_inflection(self):
        document = Document()
        p = document.add_paragraph()
        p.add_run('Ko').bold = True
        p.add_run('ty').italic = True
        p.add_run(' kot kota')
        data = payload(document)
        options = {'long_sentences': False, 'long_paragraphs': False}
        marked = Document(color_document(BytesIO(data), min_word_length=3, ignored_words='kota',
                                         analysis_options=options))
        self.assertEqual(marked.paragraphs[0].text, p.text)
        self.assertTrue(all(r.font.color.rgb is None and r.font.highlight_color is None
                            for r in marked.paragraphs[0].runs))
        marked = Document(color_document(BytesIO(data), min_word_length=3, analysis_options=options))
        self.assertEqual(marked.paragraphs[0].runs[0].font.color.rgb, marked.paragraphs[0].runs[1].font.color.rgb)
        self.assertIsNotNone(marked.paragraphs[0].runs[0].font.color.rgb)

    def test_priority_tracked_words_and_existing_underline(self):
        document = Document()
        document.add_paragraph().add_run('Kot kota').underline = True
        marked = Document(color_document(BytesIO(payload(document)), min_word_length=3, tracked_words='kot',
                                         analysis_options={'long_sentences': False, 'paragraph_limit': 1}))
        runs = marked.paragraphs[0].runs
        self.assertEqual(runs[0].font.highlight_color, WD_COLOR_INDEX.BRIGHT_GREEN)
        self.assertTrue(all(r.underline for r in runs))

    def test_extra_checks_real_sentence_parser(self):
        document = Document()
        document.add_paragraph('Kot spokojnie chodzi po domu. []')
        marked = Document(color_document(BytesIO(payload(document)),
                                         analysis_options={'sentence_limit': 2, 'paragraph_limit': 2}))
        highlights = {r.font.highlight_color for r in marked.paragraphs[0].runs}
        self.assertIn(WD_COLOR_INDEX.TURQUOISE, highlights)
        self.assertIn(WD_COLOR_INDEX.PINK, highlights)

    def test_repetitions_are_marked_in_tables_and_footnotes(self):
        result = color_document(whole_document())
        document = Document(BytesIO(result.getvalue()))
        cell = document.tables[0].cell(0, 0).paragraphs[0]
        self.assertTrue(cell._p.xpath('.//w:color'))
        self.assertEqual(cell.text, 'Lampa świeciła,  lampa gasła.')
        self.assertIn('w:color', footnote_text(BytesIO(result.getvalue())))


class ColorPaletteTests(SimpleTestCase):
    def test_both_palettes_keep_text_and_formatting_with_correct_rgb_range(self):
        for palette, low, high in [('dark', 0, 150), ('light', 160, 220)]:
            with self.subTest(palette=palette), patch(
                    'core.services.document_repetitions._lemma_map',
                    side_effect=lambda words: {word.lower(): word.lower() for word in words}):
                document = Document(color_document(BytesIO(formatted_repetition()), color_palette=palette,
                                                   analysis_options=ONLY_REPETITIONS))
                self.assertEqual(document.paragraphs[0].text, 'Koty koty.')
                colors = font_colors(document.paragraphs[0])
                self.assertTrue(colors)
                self.assertEqual(len(set(colors)), 1)
                self.assertTrue(all(low <= channel <= high for color in colors for channel in color))
                self.assertTrue(document.paragraphs[0].runs[0].bold)
                self.assertTrue(any(run.italic for run in document.paragraphs[0].runs))

    def test_invalid_palette_rejected_by_service(self):
        with self.assertRaises(ValueError):
            color_document(BytesIO(formatted_repetition()), color_palette='invalid')


class RepetitionsWorkerTests(IsolatedProgramDirsMixin, SimpleTestCase):
    @tag('slow')
    def test_worker_roundtrip_keeps_text_and_applies_light_palette(self):
        """Proces roboczy koloruje powtórzenia jasną paletą i nie wprowadza poprawek Odkurzacza."""
        document = Document()
        document.add_paragraph('Koty  koty...')
        output, extension, _ = convert_document(
            SimpleUploadedFile('test.docx', payload(document)), [], include_docx=True, normalize=False,
            repetitions={'color_palette': 'light', 'analysis_options': {'long_sentences': False}})
        with output:
            marked = Document(output)
        self.assertEqual(extension, 'docx')
        self.assertEqual(marked.paragraphs[0].text, 'Koty  koty...')
        self.assertIsNotNone(marked.paragraphs[0].runs[0].font.color.rgb)
        colors = font_colors(marked.paragraphs[0])
        self.assertTrue(colors)
        self.assertTrue(all(160 <= channel <= 220 for color in colors for channel in color))


class RepetitionsFormTests(SimpleTestCase):
    def upload(self):
        return {'document': SimpleUploadedFile('test.docx', formatted_repetition())}

    def test_form_rejects_bad_parameters_and_defaults_match_desktop(self):
        form = RepetitionsForm({**REPETITION_PARAMETERS, 'window_size': '0'}, self.upload())
        self.assertFalse(form.is_valid())
        self.assertIn('window_size', form.errors)
        self.assertTrue(RepetitionsForm().fields['duplicates'].initial)

    def test_form_accepts_light_and_defaults_missing_palette_to_dark(self):
        for palette in ('light', ''):
            form = RepetitionsForm({**REPETITION_PARAMETERS, 'color_palette': palette}, self.upload())
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.analysis_config()['color_palette'], palette or 'dark')
        form = RepetitionsForm({**REPETITION_PARAMETERS, 'color_palette': 'invalid'}, self.upload())
        self.assertFalse(form.is_valid())
        self.assertIn('color_palette', form.errors)


class RepetitionsPageTests(IsolatedProgramDirsMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('reader', 'Redaktor')

    def test_member_download_and_separate_form(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('core:programs'))
        self.assertContains(response, 'Kolorowanie powtórzeń')
        self.assertContains(response, 'name="repetitions-window_size"')
        self.assertContains(response, 'name="repetitions-color_palette"')
        document = Document()
        document.add_paragraph('Koty koty')
        with patch('core.services.program_jobs.convert_document',
                   return_value=(BytesIO(payload(document)), 'docx', 'application/octet-stream')) as convert:
            response = run_program(self.client, {
                'program_action': 'repetitions',
                'repetitions-document': SimpleUploadedFile('test.docx', payload(document)),
                'repetitions-color_palette': 'light',
                **REPETITION_FIELDS,
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn('_powtorzenia.docx', response['Content-Disposition'])
        self.assertFalse(convert.call_args.kwargs['normalize'])
        self.assertEqual(convert.call_args.kwargs['repetitions']['color_palette'], 'light')
        self.assertNotIn('use_cleaner', convert.call_args.kwargs)
        self.assertEqual(downloaded_document(response).paragraphs[0].text, 'Koty koty')
