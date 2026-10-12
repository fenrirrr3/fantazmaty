"""Dopasowanie akapitów DOCX do HTML i części dokumentu: twardy łącznik, pola tekstowe (mc:Choice i
mc:Fallback), podział strony przed dalszymi znacznikami i przed listą oraz nagłówek wspólny dla kilku sekcji."""
from django.test import SimpleTestCase
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from lxml import html

from core.services.document_html import docx_paragraph_key, paragraph_queues, split_page_breaks
from core.services.document_stories import document_stories

from .support import add_text_box


def blocks(root):
    return [html.tostring(child, encoding='unicode') for child in root]


class ParagraphKeyTests(SimpleTestCase):
    def test_hyphen_characters_are_part_of_the_key(self):
        paragraph = parse_xml(f'<w:p {nsdecls("w")}><w:r><w:t>e</w:t><w:noBreakHyphen/><w:t>mail</w:t>'
                              '<w:softHyphen/><w:t>owy</w:t></w:r></w:p>')
        self.assertEqual(docx_paragraph_key(paragraph), 'e\u2011mail\u00adowy')

    def test_text_box_is_matched_by_its_fallback_copy_only(self):
        document = Document()
        add_text_box(document.add_paragraph('Przed polem.'), 'W polu')
        queues = paragraph_queues(document)
        # Akapit pola tekstowego liczy się raz (kopia w mc:Choice jest pomijana), a akapit z polem – bez jego treści.
        self.assertEqual({key: len(queue) for key, queue in queues.items()}, {'Przed polem.': 1, 'W polu': 1})


class PageBreakSplitTests(SimpleTestCase):
    def test_markup_after_the_break_moves_to_the_next_block(self):
        root = html.fragment_fromstring('<p>A<br class="page-break"><strong>B</strong> C</p>', create_parent='div')
        split_page_breaks(root)
        self.assertEqual(blocks(root), ['<p>A</p>', '<p data-page-break="1"><strong>B</strong> C</p>'])

    def test_list_after_a_break_starts_the_new_page(self):
        root = html.fragment_fromstring('<p>Koniec<br class="page-break"></p><ul><li>Punkt</li></ul>',
                                        create_parent='div')
        split_page_breaks(root)
        self.assertEqual(blocks(root), ['<p>Koniec</p>', '<ul data-page-break="1"><li>Punkt</li></ul>'])


class DocumentStoriesTests(SimpleTestCase):
    def test_fallback_paragraphs_are_optional_and_shared_parts_are_read_once(self):
        document = Document()
        add_text_box(document.add_paragraph('Przed polem.'), 'W polu')
        header = document.sections[0].header
        header.paragraphs[0].text = 'Nagłówek'
        # Druga relacja do tej samej części (np. ten sam nagłówek dla stron parzystych).
        document.part.rels.add_relationship(RT.HEADER, header.part, 'rIdShared')
        for include_fallback, expected in ((True, ['Przed polem.', 'W polu', 'W polu']),
                                           (False, ['Przed polem.', 'W polu'])):
            with self.subTest(include_fallback=include_fallback):
                stories, _ = document_stories(document, include_fallback=include_fallback)
                self.assertEqual([story.name for story in stories], ['body', 'header'])
                self.assertEqual([p.text for p in stories[0].paragraphs], expected)
                self.assertEqual([p.text for p in stories[1].paragraphs], ['Nagłówek'])
