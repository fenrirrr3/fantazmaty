"""Reguły korekty Odkurzacza na poziomie tekstu oraz pola i limity formularzy programów."""
import time
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from django.test import SimpleTestCase
from docx import Document

from core.odkurzacz_forms import DocumentConversionForm, OdkurzaczForm, RepetitionsForm
from core.services.odkurzacz import (
    DEFAULT_EDITORIAL_RULES, EDITORIAL_RULES, _edit_opcodes, clean_docx, correct_editorial_text,
)

from .support import REPETITION_PARAMETERS, saved, sized_docx, upload


class EditorialTextRuleTests(SimpleTestCase):
    def fix(self, text):
        return correct_editorial_text(text, DEFAULT_EDITORIAL_RULES)

    def test_protected_abbreviations_keep_internal_spacing(self):
        """Regresja: skróty typu „o.o.” były rozbijane albo dostawały odstęp jak po zdaniu."""
        for abbreviation in ('ś.p.', 'c.d.n.', 'o.o.', 'j.w.'):
            with self.subTest(abbreviation=abbreviation):
                self.assertIn(abbreviation, self.fix(f'To {abbreviation} przykład.'))
        self.assertEqual(correct_editorial_text('To o.o. przykład.', ['after_punct']), 'To o.o. przykład.')

    def test_period_stays_inside_quote_when_the_sentence_continues(self):
        self.assertEqual(self.fix('Napisał „tak.” i wyszedł.'), 'Napisał „tak.” i\u00a0wyszedł.')
        self.assertEqual(self.fix('Powiedział „tak.”'), 'Powiedział „tak”.')
        self.assertEqual(self.fix('Napisał „tak.” Potem wyszedł.'), 'Napisał „tak”. Potem wyszedł.')

    def test_letter_marks_after_numbers_are_not_units(self):
        self.assertEqual(self.fix('Klasy 2B i 3A wyszły.'), 'Klasy 2B i\u00a03A wyszły.')
        self.assertEqual(self.fix('Telewizor 4K.'), 'Telewizor 4K.')
        self.assertEqual(self.fix('Kabel 2m, 5kg i 20 K.'), 'Kabel 2\u00a0m, 5\u00a0kg i\u00a020\u00a0K.')

    def test_gender_forms_in_brackets_stay_joined(self):
        self.assertEqual(self.fix('Drogi(a) Kliencie, zrobił(a) to.'), 'Drogi(a) Kliencie, zrobił(a) to.')
        self.assertEqual(self.fix('Pies(kundel) szczekał.'), 'Pies (kundel) szczekał.')

    def test_score_keeps_spaces_around_colon(self):
        self.assertEqual(self.fix('Wynik 3 : 1 , a potem :'), 'Wynik 3 : 1, a\u00a0potem:')

    def test_partial_fragment_is_not_a_new_dialogue(self):
        rules = ['hyphen_dash', 'dash_spaces', 'trim']
        self.assertEqual(correct_editorial_text(' – narracja', rules, trim_start=False), ' – narracja')
        self.assertEqual(correct_editorial_text(' - narracja', rules, trim_start=False), ' – narracja')
        self.assertEqual(correct_editorial_text('tekst – ', rules, trim_end=False), 'tekst – ')
        self.assertEqual(correct_editorial_text('  –Hę?', rules), '– Hę?')

    def test_typographic_rules_are_available_and_can_be_disabled(self):
        added = {'single_nbsp', 'unit_nbsp', 'reference_nbsp', 'dates_times', 'decimal', 'digit_groups'}
        self.assertTrue(added <= {key for key, _ in EDITORIAL_RULES})
        source = '2025-1-2 2/1/2025 o 9.30 9:30 12.50 kg 123456 kg A.Kowalski 20°C'
        self.assertEqual(correct_editorial_text(source, []), source)
        self.assertIn('\u00a0', correct_editorial_text(source))

    def test_defaults_and_explicit_empty_paragraph_rule(self):
        self.assertEqual(correct_editorial_text('kot\tkota', {'tabs'}), 'kot kota')
        self.assertEqual(correct_editorial_text('1.02.2025', {'dates_times'}), '01.02.2025')
        self.assertNotIn('empty_paragraphs', DEFAULT_EDITORIAL_RULES)
        self.assertNotIn('user_word_corrections', DEFAULT_EDITORIAL_RULES)
        self.assertEqual(
            correct_editorial_text('np. kot. pies! – powiedział.', {'sentence_case'}),
            'np. kot. Pies! – powiedział.',
        )
        document = Document()
        document.add_paragraph('A')
        document.add_paragraph()
        document.add_paragraph()
        document.add_paragraph('B')
        payload = saved(document).getvalue()
        marked = Document(clean_docx(BytesIO(payload), ['empty_paragraphs']))
        self.assertEqual([p.text for p in marked.paragraphs], ['A', '', 'B'])
        marked = Document(clean_docx(BytesIO(payload), DEFAULT_EDITORIAL_RULES))
        self.assertEqual(len(marked.paragraphs), 4)

    def test_edit_opcodes_handle_the_longest_paragraph_quickly(self):
        source = ('Ala  ma kota a kot ma Alę... ' * 700)[:20000]
        corrected = self.fix(source)
        started = time.monotonic()
        opcodes = _edit_opcodes(source, corrected)
        self.assertLess(time.monotonic() - started, 2)
        rebuilt = ''.join(source[a:b] if tag == 'equal' else corrected[c:d]
                          for tag, a, b, c, d in opcodes if tag != 'delete')
        self.assertEqual(rebuilt, corrected)


class ProgramFormTests(SimpleTestCase):
    def test_cleaner_rule_defaults_and_order(self):
        self.assertEqual(EDITORIAL_RULES[-1][0], 'empty_paragraphs')
        form = OdkurzaczForm()
        initial = set(form.fields['rules'].initial)
        self.assertNotIn('empty_paragraphs', initial)
        self.assertTrue({key for key, *_ in EDITORIAL_RULES} - {'empty_paragraphs'} <= initial)
        self.assertTrue(form.fields['remove_soft_whitespace'].initial)
        # Automatyczne czyszczenie poczty zachowuje dotychczasowe reguły.
        self.assertTrue(DEFAULT_EDITORIAL_RULES)

    def test_only_the_cleaner_offers_formatting_and_soft_whitespace_options(self):
        self.assertNotIn('remove_soft_whitespace', DocumentConversionForm(prefix='convert').fields)
        self.assertNotIn('normalize_formatting', DocumentConversionForm().fields)
        self.assertNotIn('normalize_formatting', RepetitionsForm().fields)

    def test_all_program_forms_accept_two_mb_and_reject_one_extra_byte(self):
        data = {'formats': ['pdf'], **REPETITION_PARAMETERS}
        for form_class in (OdkurzaczForm, DocumentConversionForm, RepetitionsForm):
            with self.subTest(form=form_class.__name__):
                form = form_class(data, {'document': sized_docx(2 * 1024 * 1024)})
                self.assertTrue(form.is_valid(), form.errors)
                form = form_class(data, {'document': sized_docx(2 * 1024 * 1024 + 1)})
                self.assertFalse(form.is_valid())
                self.assertIn('2 MB', str(form.errors['document']))

    def test_mailbox_validator_can_keep_its_existing_ten_mb_limit(self):
        form = OdkurzaczForm({}, {'document': sized_docx(2 * 1024 * 1024 + 1)},
                             max_document_bytes=10 * 1024 * 1024)
        self.assertTrue(form.is_valid(), form.errors)

    def test_zip_expansion_limit(self):
        stream = BytesIO()
        with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', b'a' * (51 * 1024 * 1024))
            archive.writestr('[Content_Types].xml', b'')
        form = OdkurzaczForm({}, {'document': upload(stream.getvalue())})
        self.assertFalse(form.is_valid())
        self.assertIn('50 MB', str(form.errors))
