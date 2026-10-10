"""Wszystkie fragmenty tekstu DOCX, które czyta człowiek.

Treść główna (razem z tabelami i polami tekstowymi), nagłówki, stopki,
przypisy dolne i końcowe. Komentarze recenzentów pomijamy celowo.
Moduł działa także w izolowanym konwerterze, bez kontekstu pakietu.
"""
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml import etree

FALLBACK = '{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback'
SECONDARY = (RT.HEADER, RT.FOOTER, RT.FOOTNOTES, RT.ENDNOTES)


class Story:
    def __init__(self, name, root, paragraphs):
        self.name, self.root, self.paragraphs = name, root, paragraphs


class _Parent:
    """Rodzic akapitu spoza treści głównej; python-docx potrzebuje tylko .part."""

    def __init__(self, part):
        self.part = part


def _paragraphs(root, parent, include_fallback):
    result = []
    for element in root.iter(qn('w:p')):
        # Pola tekstowe mają kopię zapasową dla starszych programów (mc:Fallback).
        if not include_fallback and any(node.tag == FALLBACK for node in element.iterancestors()):
            continue
        result.append(Paragraph(element, parent))
    return result


def document_stories(document, *, include_fallback=True):
    """Zwróć (lista Story, commit). commit() zapisuje zmiany w przypisach.

    Przypisy python-docx wczytuje jako surowe bajty, więc edytujemy ich kopię
    XML i odkładamy ją do paczki dopiero po zakończeniu zmian.
    """
    stories = [Story('body', document.element.body,
                     _paragraphs(document.element.body, document._body, include_fallback))]
    pending, seen = [], set()
    for relationship in document.part.rels.values():
        if relationship.is_external or relationship.reltype not in SECONDARY:
            continue
        part = relationship.target_part
        if id(part) in seen:
            continue
        seen.add(id(part))
        root = getattr(part, 'element', None)
        if root is None:
            root = parse_xml(part.blob)
            pending.append((part, root))
        stories.append(Story(relationship.reltype.rsplit('/', 1)[-1], root,
                             _paragraphs(root, _Parent(part), include_fallback)))

    def commit():
        for part, root in pending:
            part._blob = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

    return stories, commit
