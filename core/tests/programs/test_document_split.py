"""Podział DOCX według Nagłówków 1 (tylko superuser): pliki opowiadań w ZIP, tylko własne przypisy, obrazy i komentarze."""
from contextlib import nullcontext
from io import BytesIO
from unittest.mock import patch
from urllib.parse import unquote
from zipfile import ZIP_DEFLATED, ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from docx import Document

from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from lxml import etree

from core.program_test_support import run_program
from core.services import program_jobs as jobs
from core.services.document_split import NoHeadingError, file_name, heading_style_ids, split_document
from fantazmaty.testing import create_superuser

from .support import IsolatedProgramDirsMixin, add_footnote_reference, docx_bytes, downloaded, png, saved

STORIES = (('Pierwsze opowiadanie', (20, 10)), ('Drugie: „cytat”?', (30, 15)), ('Trzecie', None))
EXPECTED = ['01 Pierwsze opowiadanie.docx', '02 Drugie „cytat”.docx', '03 Trzecie.docx']


def with_footnotes(payload, texts):
    """Dodaje do DOCX część przypisów dolnych o identyfikatorach 1, 2, … (python-docx ich nie tworzy)."""
    notes = ''.join(f'<w:footnote w:id="{number}"><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:footnote>'
                    for number, text in enumerate(texts, 1))
    footnotes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:footnotes '
                 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                 '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
                 f'{notes}</w:footnotes>')
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == '[Content_Types].xml':
                data = data.replace(b'</Types>', b'<Override PartName="/word/footnotes.xml" ContentType="application/'
                                    b'vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>')
            if item.filename == 'word/_rels/document.xml.rels':
                data = data.replace(b'</Relationships>', b'<Relationship Id="rIdFoot" Type="http://schemas.openxml'
                                    b'formats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>'
                                    b'</Relationships>')
            target.writestr(item, data)
        target.writestr('word/footnotes.xml', footnotes)
    return output.getvalue()


def anthology(intro=True):
    """Strona tytułowa i trzy opowiadania; dwa pierwsze mają obraz, każde przypis i komentarz."""
    document = Document()
    if intro:
        document.add_paragraph('Antologia „Zbiór” – strona tytułowa')
    for number, (title, image) in enumerate(STORIES, 1):
        document.add_heading(title, level=1)
        paragraph = document.add_paragraph(f'Treść opowiadania {number}.')
        add_footnote_reference(paragraph, number)
        if image:
            paragraph.add_run().add_picture(png(*image))
        document.add_comment(paragraph.runs[0], text=f'Komentarz opowiadania {number}', author='Redakcja')
    return with_footnotes(saved(document).getvalue(), [f'Przypis opowiadania {n}' for n in range(1, 4)])


def parts(payload):
    with ZipFile(BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def rewrite(payload, replace=None, add=None, drop=()):
    """Kopia DOCX ze zmienionymi (funkcja bajty → bajty), dodanymi i usuniętymi częściami pakietu."""
    replace, add = replace or {}, add or {}
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for item in source.infolist():
            if item.filename not in drop:
                data = source.read(item.filename)
                target.writestr(item, replace[item.filename](data) if item.filename in replace else data)
        for name, data in add.items():
            target.writestr(name, data)
    return output.getvalue()


def add_relationships(*relationships):
    """Funkcja dopisująca relacje (Id, Type, Target[, TargetMode]) do pliku .rels."""
    def change(data):
        extra = ''.join(f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"'
                        + (f' TargetMode="{mode[0]}"' if mode else '') + '/>'
                        for rid, kind, target, *mode in relationships)
        return data.replace(b'</Relationships>', extra.encode() + b'</Relationships>')
    return change


def split(payload):
    """split_document wywołany w tym procesie (coverage widzi kod, który zwykle działa w podprocesie)."""
    output = BytesIO()
    report = split_document(BytesIO(payload), output)
    return report, parts(output.getvalue())


def stories(*titles, intro=None):
    """DOCX z akapitem wstępu (opcjonalnie) i opowiadaniami: Nagłówek 1 z tytułem, akapit treści."""
    document = Document()
    if intro is not None:
        document.add_paragraph(intro)
    for number, title in enumerate(titles, 1):
        document.add_heading(title, level=1)
        document.add_paragraph(f'Treść {number}.')
    return document


def assert_story_files(test, files):
    """Pliki z anthology(): każdy ma tylko własną treść, przypis, obraz i komentarz (i żadnej miniatury)."""
    test.assertEqual(list(files), EXPECTED)
    for number, name in enumerate(EXPECTED, 1):
        with test.subTest(name=name):
            package = parts(files[name])
            document = Document(BytesIO(files[name]))
            test.assertEqual([paragraph.text for paragraph in document.paragraphs],
                             [STORIES[number - 1][0], f'Treść opowiadania {number}.'])
            everything = b''.join(package.values()).decode('utf-8', 'replace')
            test.assertNotIn('strona tytułowa', everything)
            for other in {1, 2, 3} - {number}:
                test.assertNotIn(f'Przypis opowiadania {other}', package['word/footnotes.xml'].decode())
                test.assertNotIn(f'opowiadania {other}', everything)
            test.assertIn(f'Przypis opowiadania {number}', package['word/footnotes.xml'].decode())
            test.assertIn(f'Komentarz opowiadania {number}', package['word/comments.xml'].decode())
            media = [part for part in package if part.startswith('word/media/')]
            test.assertEqual(len(media), 1 if STORIES[number - 1][1] else 0)
            if media:
                from PIL import Image
                test.assertEqual(Image.open(BytesIO(package[media[0]])).size, STORIES[number - 1][1])
            test.assertEqual(package['word/_rels/document.xml.rels'].count(b'media/'), len(media))
            # Miniatura pierwszej strony pokazuje początek całej antologii.
            test.assertNotIn('docProps/thumbnail.jpeg', list(package))
            test.assertNotIn(b'thumbnail', package['_rels/.rels'])


class SplitHelpersTests(SimpleTestCase):
    def test_heading_one_is_recognised_by_style_name_or_id(self):
        """Nagłówek 1 rozpoznajemy po nazwie stylu (bez względu na wielkość liter) albo po id Heading1/Nagwek1."""
        def styles(style_id, name):
            return (f'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/></w:style>'
                    f'</w:styles>').encode()
        for style_id, name, expected in (('Tytuopowiadania', 'heading 1', True),
                                         ('Moj', 'Nagłówek 1', True), ('NAGWEK1', 'cokolwiek', True),
                                         ('Heading2', 'heading 2', False)):
            with self.subTest(style_id=style_id, name=name):
                self.assertEqual(style_id in heading_style_ids(styles(style_id, name)), expected)

    def test_file_names_are_numbered_cleaned_and_shortened(self):
        """Nazwa pliku: numer z zerem wiodącym, tytuł bez znaków niedozwolonych, skrócony; pusty tytuł zastąpiony."""
        for number, width, title, expected in ((1, 2, 'A/B: C?', '01 A B C.docx'), (7, 3, '  ...  ', '007 Opowiadanie.docx'),
                                               (2, 2, 'x' * 200, '02 ' + 'x' * 80 + '.docx')):
            with self.subTest(title=title):
                self.assertEqual(file_name(number, width, title), expected)


W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
MS_REL = 'http://schemas.microsoft.com/office'


class SplitDocumentTests(SimpleTestCase):
    """split_document() w tym procesie: rozpoznanie nagłówków, nazwy plików, części pakietu i przypadki brzegowe."""

    def test_anthology_is_split_into_story_files(self):
        """Antologia z przypisami, obrazami i komentarzami: pliki opowiadań i informacja o pominiętym wstępie."""
        for intro in (True, False):
            with self.subTest(intro=intro):
                report, files = split(anthology(intro))
                self.assertEqual(report, {'files': EXPECTED, 'skipped_intro': intro})
                assert_story_files(self, files)

    def test_heading_one_is_found_by_style_name_id_or_default_id(self):
        """Nagłówek 1: własny styl o nazwie „Nagłówek 1”, polskie id Nagwek1 albo Heading1 przy braku części stylów."""
        def renamed(style_id, name):
            return {'word/styles.xml': lambda data: data.replace(b'w:styleId="Heading1"', f'w:styleId="{style_id}"'.encode())
                    .replace(b'<w:name w:val="heading 1"/>', f'<w:name w:val="{name}"/>'.encode()),
                    'word/document.xml': lambda data: data.replace(b'w:val="Heading1"', f'w:val="{style_id}"'.encode())}
        payload = saved(stories('Jeden', 'Dwa')).getvalue()
        cases = (
            ('własny styl', rewrite(payload, renamed('TytulOpowiadania', 'Nagłówek 1'))),
            ('polski Word', rewrite(payload, renamed('Nagwek1', 'Nagłówek 1'))),
            ('bez stylów', rewrite(payload, {'word/_rels/document.xml.rels': lambda data: data.replace(
                b'relationships/styles"', b'relationships/x-styles"')})),
        )
        for label, variant in cases:
            with self.subTest(label):
                report, files = split(variant)
                self.assertEqual(report['files'], ['01 Jeden.docx', '02 Dwa.docx'])
                self.assertEqual(Document(BytesIO(files['02 Dwa.docx'])).paragraphs[1].text, 'Treść 2.')

    def test_document_without_heading_one_or_main_part_is_rejected(self):
        """Bez Nagłówka 1 (także bez treści) – NoHeadingError; pakiet bez głównej części – błąd zamiast pustego ZIP."""
        heading_two = Document()
        heading_two.add_heading('Rozdział', level=2)
        heading_two.add_paragraph('Tekst.')
        cases = (
            ('same akapity', docx_bytes('Sam tekst.'), NoHeadingError),
            ('Nagłówek 2', saved(heading_two).getvalue(), NoHeadingError),
            ('bez w:body', rewrite(docx_bytes(), {'word/document.xml': lambda data: data.replace(
                b'<w:body>', b'<w:x>').replace(b'</w:body>', b'</w:x>')}), NoHeadingError),
            ('bez relacji pakietu', rewrite(docx_bytes(), drop={'_rels/.rels'}), ValueError),
        )
        for label, payload, error in cases:
            with self.subTest(label), self.assertRaises(error):
                split(payload)

    def test_file_names_for_empty_duplicate_long_and_tabbed_titles(self):
        """Pusty nagłówek, powtórzony tytuł, bardzo długi tytuł i tabulator – unikalne, poprawne nazwy plików."""
        document = stories('', 'Ten sam', 'Ten sam', 'Ż' * 120)
        document.add_heading('Z\ttabem', level=1)
        report, files = split(saved(document).getvalue())
        self.assertEqual(report['files'], ['01 Opowiadanie.docx', '02 Ten sam.docx', '03 Ten sam.docx',
                                           '04 ' + 'Ż' * 80 + '.docx', '05 Z tabem.docx'])
        self.assertEqual(list(files), report['files'])
        self.assertEqual(Document(BytesIO(files['03 Ten sam.docx'])).paragraphs[1].text, 'Treść 3.')

    def test_intro_counts_as_skipped_only_when_visible(self):
        """Wstęp z samym obrazem jest pomijaną treścią; puste akapity przed pierwszym nagłówkiem – nie."""
        empty = stories('Jedno', intro='')
        empty.paragraphs[0].add_run('\t')
        picture = stories('Jedno', intro='')
        picture.paragraphs[0].add_run().add_picture(png(4, 4))
        for label, document, skipped in (('puste', empty, False), ('obraz', picture, True)):
            with self.subTest(label):
                report, files = split(saved(document).getvalue())
                self.assertEqual(report['skipped_intro'], skipped)
                self.assertFalse([name for name in parts(files['01 Jedno.docx']) if name.startswith('word/media/')])

    def test_story_keeps_its_table_link_and_page_header_only(self):
        """Tabela, hiperłącze i komentarz XML zostają w swoim opowiadaniu; nagłówek strony (z ustawień sekcji) – w każdym pliku."""
        document = stories('Z tabelą', 'Bez tabeli')
        document.sections[0].header.paragraphs[0].text = 'Żywa pagina'
        heading = document.paragraphs[2]
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = 'Komórka'
        heading._p.addprevious(table._tbl)
        link = document.part.relate_to('https://example.com/opowiadanie', f'{REL}/hyperlink', is_external=True)
        document.paragraphs[1]._p.append(parse_xml(
            f'<w:hyperlink {nsdecls("w", "r")} r:id="{link}"><w:r><w:t>link</w:t></w:r></w:hyperlink>'))
        document.paragraphs[1]._p.append(etree.Comment(' komentarz XML '))
        _, files = split(saved(document).getvalue())
        first, second = (parts(files[name]) for name in ('01 Z tabelą.docx', '02 Bez tabeli.docx'))
        self.assertIn(b'Kom\xc3\xb3rka', first['word/document.xml'])
        self.assertNotIn(b'<w:tbl>', second['word/document.xml'])
        self.assertIn(b'example.com/opowiadanie', first['word/_rels/document.xml.rels'])
        self.assertNotIn(b'example.com', second['word/_rels/document.xml.rels'])
        self.assertIn(b'<!-- komentarz XML -->', first['word/document.xml'])
        for package in (first, second):
            header = next(name for name in package if name.startswith('word/header'))
            self.assertIn('Żywa pagina', package[header].decode())

    def test_absolute_targets_and_document_without_section_properties(self):
        """Relacje z bezwzględną ścieżką (/word/…) i dokument bez końcowego w:sectPr dzielą się poprawnie."""
        payload = rewrite(saved(stories('Jedno', 'Dwa')).getvalue(), {
            '_rels/.rels': lambda data: data.replace(b'Target="word/document.xml"', b'Target="/word/document.xml"'),
            'word/_rels/document.xml.rels': lambda data: data.replace(b'Target="styles.xml"',
                                                                      b'Target="/word/styles.xml"'),
            'word/document.xml': lambda data: etree.tostring(_without_section(data)),
        })
        report, files = split(payload)
        self.assertEqual(report['files'], ['01 Jedno.docx', '02 Dwa.docx'])
        package = parts(files['02 Dwa.docx'])
        self.assertIn('word/styles.xml', package)
        self.assertNotIn(b'sectPr', package['word/document.xml'])
        self.assertEqual([p.style.name for p in Document(BytesIO(files['02 Dwa.docx'])).paragraphs],
                         ['Heading 1', 'Normal'])

    def test_endnotes_and_modern_comment_parts_keep_only_own_entries(self):
        """Przypisy końcowe i części komentarzy Worda 2013+ (commentsExtended/Ids/Extensible) – tylko wpisy opowiadania;
        relacja do brakującej części nie przerywa podziału."""
        document = stories('Jedno', 'Dwa')
        for number, paragraph in ((1, document.paragraphs[1]), (2, document.paragraphs[3])):
            paragraph._p.append(parse_xml(f'<w:r {nsdecls("w")}><w:endnoteReference w:id="{number}"/></w:r>'))
            document.add_comment(paragraph.runs[0], text=f'Uwaga {number}', author='Redakcja')
        endnotes = ''.join(f'<w:endnote w:id="{n}"><w:p><w:r><w:t>Końcowy {n}</w:t></w:r></w:p></w:endnote>'
                           for n in (1, 2))

        def para_ids(data):
            root = etree.fromstring(data)
            for comment in root.iter(f'{{{W_NS}}}comment'):
                comment.find(f'{{{W_NS}}}p').set('{http://schemas.microsoft.com/office/word/2010/wordml}paraId',
                                                 f'0000000{comment.get(f"{{{W_NS}}}id")}')
            return etree.tostring(root)
        w15, cid, cex = (f'{MS_REL}/word/2012/wordml', f'{MS_REL}/word/2016/wordml/cid',
                         f'{MS_REL}/word/2018/wordml/cex')
        payload = rewrite(saved(document).getvalue(), {
            'word/comments.xml': para_ids,
            'word/_rels/document.xml.rels': add_relationships(
                ('rIdEnd', f'{REL}/endnotes', 'endnotes.xml'),
                ('rIdEx', f'{MS_REL}/2011/relationships/commentsExtended', 'commentsExtended.xml'),
                ('rIdIds', f'{MS_REL}/2016/09/relationships/commentsIds', 'commentsIds.xml'),
                ('rIdCex', f'{MS_REL}/2018/08/relationships/commentsExtensible', 'commentsExtensible.xml'),
                ('rIdMissing', f'{MS_REL}/2016/09/relationships/x-commentsIds', 'brak.xml'),
                ('rIdGlossary', f'{REL}/glossaryDocument', 'glossary/brak.xml')),
        }, add={
            'word/endnotes.xml': f'<w:endnotes xmlns:w="{W_NS}"><w:endnote w:type="separator" w:id="-1"><w:p/>'
                                 f'</w:endnote>{endnotes}</w:endnotes>',
            'word/commentsExtended.xml': f'<w15:commentsEx xmlns:w15="{w15}"><w15:commentEx w15:paraId="00000000"/>'
                                         '<w15:commentEx w15:paraId="00000001"/></w15:commentsEx>',
            'word/commentsIds.xml': f'<w16cid:commentsIds xmlns:w16cid="{cid}"><!-- Word -->'
                                    '<w16cid:commentId w16cid:paraId="00000000" w16cid:durableId="0AAA0000"/>'
                                    '<w16cid:commentId w16cid:paraId="00000001" w16cid:durableId="0BBB0000"/>'
                                    '</w16cid:commentsIds>',
            'word/commentsExtensible.xml': f'<w16cex:commentsExtensible xmlns:w16cex="{cex}"><!-- Word -->'
                                           '<w16cex:commentExtensible w16cex:durableId="0AAA0000"/>'
                                           '<w16cex:commentExtensible w16cex:durableId="0BBB0000"/>'
                                           '</w16cex:commentsExtensible>',
        })
        _, files = split(payload)
        for name, own, other in (('01 Jedno.docx', ('00000000', '0AAA0000', 'Uwaga 1', 'Końcowy 1'),
                                  ('00000001', '0BBB0000', 'Uwaga 2', 'Końcowy 2')),
                                 ('02 Dwa.docx', ('00000001', '0BBB0000', 'Uwaga 2', 'Końcowy 2'),
                                  ('00000000', '0AAA0000', 'Uwaga 1', 'Końcowy 1'))):
            with self.subTest(name):
                package = parts(files[name])
                text = ''.join(package[part].decode() for part in (
                    'word/comments.xml', 'word/commentsExtended.xml', 'word/commentsIds.xml',
                    'word/commentsExtensible.xml', 'word/endnotes.xml'))
                for value in own:
                    self.assertIn(value, text)
                for value in other:
                    self.assertNotIn(value, text)
                self.assertIn('w:type="separator"', package['word/endnotes.xml'].decode())


def _without_section(data):
    root = etree.fromstring(data)
    body = root.find(f'{{{W_NS}}}body')
    body.remove(body.find(f'{{{W_NS}}}sectPr'))
    return root


@tag('slow')
class SplitProgramTests(IsolatedProgramDirsMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('podzial-admin')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def run_split(self, payload):
        return run_program(self.client, {'program_action': 'split',
                                         'split-document': SimpleUploadedFile('Antologia.docx', payload)})

    def test_each_story_gets_only_its_own_content_notes_images_and_comments(self):
        """3 opowiadania + strona tytułowa → 3 pliki w ZIP; w każdym tylko własna treść, przypis, obraz i komentarz."""
        response = self.run_split(anthology())
        self.assertIn('Antologia – podział.zip', unquote(response['Content-Disposition']))
        assert_story_files(self, parts(downloaded(response)))

    def test_skipped_title_page_and_file_list_are_reported(self):
        """Podsumowanie wymienia utworzone pliki i informuje o pominiętej treści przed pierwszym Nagłówkiem 1."""
        for intro in (True, False):
            with self.subTest(intro=intro), patch.object(jobs, 'launch', side_effect=jobs.execute):
                response = self.client.post(reverse('core:programs'), {
                    'program_action': 'split', 'split-document': SimpleUploadedFile('A.docx', anthology(intro))},
                    HTTP_X_PROGRAM_JOB='1')
                state = self.client.get(response.json()['url']).json()
                self.assertEqual(state['state'], 'done', state)
                self.assertEqual(state['warnings'][0], 'Utworzono 3 pliki: ' + ', '.join(EXPECTED) + '.')
                self.assertEqual(any('Pominięto treść przed pierwszym Nagłówkiem 1' in text
                                     for text in state['warnings']), intro)
                downloaded(self.client.get(response.json()['url'] + '?download=1'))

    def test_converter_result_is_checked_before_download(self):
        """Wynik podprocesu: brak lub uszkodzony split.json, pusta lista albo brak archiwum – błąd; jeden plik – liczba pojedyncza."""
        def converter(report, archive=b'PK'):
            def run(directory, limit, control):
                if report is not None:
                    (directory / 'split.json').write_text(report, encoding='utf-8')
                if archive:
                    (directory / 'document.zip').write_bytes(archive)
            return run
        broken = 'Podział nie utworzył poprawnego archiwum lub wynik przekroczył 50 MB.'
        one = '{"files": ["01 A.docx"], "skipped_intro": false}'
        cases = (
            ('bez raportu', converter(None), 'error', broken),
            ('uszkodzony raport', converter('{'), 'error', broken),
            ('pusta lista', converter('{"files": []}'), 'error', broken),
            ('bez archiwum', converter(one, archive=None), 'error', broken),
            ('jeden plik', converter(one), 'done', 'Utworzono 1 plik: 01 A.docx.'),
        )
        for label, run, expected, message in cases:
            logs = self.assertLogs('core.services.program_jobs', 'ERROR') if expected == 'error' else nullcontext()
            with self.subTest(label), logs, patch.object(jobs, 'run_converter', side_effect=run), \
                    patch.object(jobs, 'launch', side_effect=jobs.execute):
                response = self.client.post(reverse('core:programs'), {
                    'program_action': 'split', 'split-document': SimpleUploadedFile('A.docx', anthology())},
                    HTTP_X_PROGRAM_JOB='1')
                state = self.client.get(response.json()['url']).json()
                self.assertEqual(state['state'], expected, state)
                self.assertEqual(state['message'] if expected == 'error' else state['warnings'][0], message)
                if expected == 'done':
                    self.assertEqual(downloaded(self.client.get(response.json()['url'] + '?download=1')), b'PK')

    def test_document_without_heading_one_is_an_error(self):
        """Bez akapitu w stylu Nagłówek 1 nie powstaje żaden plik – komunikat po polsku."""
        response = self.run_split(docx_bytes('Sam tekst bez nagłówków.'))
        self.assertContains(response, 'nie zawiera akapitów ze stylem „Nagłówek 1”', status_code=200)
