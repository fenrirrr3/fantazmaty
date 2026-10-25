"""Wspólne pomocniki części XML pakietu DOCX: przestrzenie nazw, bezpieczny odczyt i zapis, relacje.

Tylko biblioteka standardowa i lxml – moduł działa w izolowanym podprocesie konwertera i w programie
lokalnym (tools/redaktor_docx/build.py, pierwszy na liście modułów).
"""
import posixpath

from lxml import etree

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
WORD = f'{{{W}}}'


def w(name):
    return WORD + name


def mc(name):
    return f'{{{MC}}}{name}'


# Treść alternatywna (mc:AlternateContent): wersja dla nowszych programów i kopia zapasowa dla starszych.
CHOICE = mc('Choice')
FALLBACK = mc('Fallback')


def xml_parser():
    """Parser części od użytkownika: bez rozwijania encji, sieci i DTD (osobny dla każdego odczytu)."""
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)


def parse_part(data):
    return etree.fromstring(data, xml_parser())


def serialize_part(root):
    """Część XML z deklaracją jak w plikach Worda (UTF-8, standalone)."""
    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)


def rels_path(part):
    """word/document.xml → word/_rels/document.xml.rels; '' (pakiet) → _rels/.rels."""
    folder, name = posixpath.split(part)
    return posixpath.join(folder, '_rels', name + '.rels')


def source_part(rels_name):
    """word/_rels/document.xml.rels → word/document.xml; _rels/.rels → ''."""
    folder, name = posixpath.split(rels_name)
    return posixpath.join(posixpath.dirname(folder), name[:-len('.rels')])


def is_internal(relationship):
    return relationship.get('TargetMode', '').lower() != 'external'


def part_target(part, relationship):
    """Część pakietu, do której prowadzi relacja części `part` (None dla relacji zewnętrznej).

    Target zaczynający się od „/” jest liczony od korzenia pakietu, pozostałe – od katalogu części.
    """
    if not is_internal(relationship):
        return None
    target = relationship.get('Target', '')
    if target.startswith('/'):
        return posixpath.normpath(target.lstrip('/'))
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
