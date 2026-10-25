"""Obrazy usuwane przed pracą każdego programu (także w nietypowym zapisie OOXML, z relacjami i częściami pakietu)
i język polski narzucony całemu dokumentowi."""
import json
import posixpath
import re
import sys
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from django.test import SimpleTestCase, tag
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from lxml import etree

from core.services import document_conversion_worker as worker
from core.services.document_images import force_polish, linked_images_note, strip_images, unlink_external_images

from .support import (
    IMAGE_NAMESPACES, PICTURE_DRAWING, docx_bytes, docx_with_images, docx_with_linked_images, external_images,
    footnote_text, png, saved, set_default_language, set_language, with_footnote,
)

R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def package_problems(stream):
    """Relacje bez pliku, odwołania r:… bez relacji, pliki bez relacji i wpisy typów bez pliku."""
    problems = []
    with ZipFile(stream) as archive:
        names = set(archive.namelist())
        targets = set()
        for name in names:
            if not name.endswith('.rels'):
                continue
            folder, base = posixpath.split(name)
            part = posixpath.join(posixpath.dirname(folder), base[:-len('.rels')])
            relationships = etree.fromstring(archive.read(name))
            for relationship in relationships:
                if relationship.get('TargetMode') == 'External':
                    continue
                target = posixpath.normpath(posixpath.join(posixpath.dirname(part), relationship.get('Target')))
                targets.add(target)
                if target not in names:
                    problems.append(('brak pliku', target))
            if part in names and part.endswith('.xml'):
                ids = {relationship.get('Id') for relationship in relationships}
                for node in etree.fromstring(archive.read(part)).iter():
                    problems += [('brak relacji', part, value) for key, value in node.attrib.items()
                                 if key.startswith(f'{{{R}}}') and value not in ids]
        problems += [('plik bez relacji', name) for name in names
                     if not name.endswith('.rels') and name != '[Content_Types].xml' and name not in targets]
        for entry in etree.fromstring(archive.read('[Content_Types].xml')):
            if entry.get('PartName') and entry.get('PartName').lstrip('/') not in names:
                problems.append(('typ bez pliku', entry.get('PartName')))
    stream.seek(0)
    return problems


@tag('docx')
class StripImagesTests(SimpleTestCase):
    def test_images_are_removed_from_every_part_and_counted_once(self):
        source = docx_with_images()
        self.assertEqual(package_problems(source), [])
        result, removed = strip_images(source)
        # Treść, pole tekstowe (DrawingML i zapasowy VML to jeden obraz), VML, obiekt OLE, nagłówek, przypis.
        self.assertEqual(removed, 6)
        self.assertEqual(package_problems(result), [])
        with ZipFile(result) as archive:
            names = archive.namelist()
            self.assertFalse([name for name in names if 'media/' in name or 'embeddings/' in name or 'thumbnail' in name])
            for name in names:
                if name.endswith('.xml'):
                    data = archive.read(name)
                    for marker in (b'<pic:pic', b'imagedata', b'<w:object', b'r:embed', b'oleObject'):
                        self.assertNotIn(marker, data, name)
            types = archive.read('[Content_Types].xml')
            self.assertNotIn(b'Extension="png"', types)
            body = archive.read('word/document.xml').decode()
        result.seek(0)
        document = Document(result)
        self.assertEqual([paragraph.text for paragraph in document.paragraphs],
                         ['Przed obrazem.', '', 'Pole: ', 'Po obrazie.'])
        self.assertEqual(body.count('Pole z obrazem'), 2)  # tekst pola tekstowego (oba warianty) zostaje
        self.assertEqual(document.sections[0].header.paragraphs[0].text, 'Nagłówek ')
        self.assertIn('Przypis z obrazem', footnote_text(result))

    def test_document_without_images_is_returned_unchanged(self):
        data = docx_bytes('Bez obrazów.')
        result, removed = strip_images(BytesIO(data))
        self.assertEqual((result.getvalue(), removed), (data, 0))

    def test_picture_bullets_fall_back_to_a_plain_bullet(self):
        document = Document()
        numbering = document.part.numbering_part.element
        numbering.insert(0, parse_xml(
            f'<w:numPicBullet {IMAGE_NAMESPACES} w:numPicBulletId="0"><w:pict><v:shape style="width:9pt;height:9pt">'
            f'<v:imagedata r:id="rIdBullet"/></v:shape></w:pict></w:numPicBullet>'))
        numbering.append(parse_xml(
            f'<w:abstractNum {nsdecls("w")} w:abstractNumId="7"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/>'
            '<w:lvlText w:val="•"/><w:lvlPicBulletId w:val="0"/></w:lvl></w:abstractNum>'))
        source = with_part_image(saved(document), 'word/numbering.xml', 'rIdBullet')
        self.assertEqual(package_problems(source), [])
        result, removed = strip_images(source)
        self.assertEqual(removed, 1)
        self.assertEqual(package_problems(result), [])
        with ZipFile(result) as archive:
            numbering = archive.read('word/numbering.xml')
            self.assertNotIn('word/media/bullet.png', archive.namelist())
        self.assertNotIn(b'numPicBullet', numbering)
        self.assertNotIn(b'lvlPicBulletId', numbering)
        self.assertIn('•'.encode(), numbering)

    def test_text_box_with_a_picture_fill_keeps_its_text(self):
        document = Document()
        document.add_picture(BytesIO(saved_png()))
        rid = next(rid for rid, rel in document.part.rels.items() if rel.reltype.endswith('/image'))
        document.element.body.remove(document.paragraphs[0]._p)
        paragraph = document.add_paragraph()
        paragraph._p.append(parse_xml(
            f'<w:r {IMAGE_NAMESPACES}><w:drawing><wp:inline><wp:docPr id="3" name="Pole"/><a:graphic>'
            '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"><wps:wsp>'
            f'<wps:spPr><a:blipFill><a:blip r:embed="{rid}"/></a:blipFill></wps:spPr><wps:txbx><w:txbxContent>'
            '<w:p><w:r><w:t>Tekst na obrazie</w:t></w:r></w:p></w:txbxContent></wps:txbx></wps:wsp>'
            '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'))
        result, removed = strip_images(saved(document))
        self.assertEqual(removed, 1)
        self.assertEqual(package_problems(result), [])
        with ZipFile(result) as archive:
            body = archive.read('word/document.xml').decode()
        self.assertIn('Tekst na obrazie', body)
        self.assertIn('<a:noFill/>', body)

    def test_result_opens_in_python_docx_after_every_step(self):
        result, _ = strip_images(docx_with_images())
        stripped = Document(result)
        polished = force_polish(result)
        self.assertEqual(package_problems(polished), [])
        polished = Document(polished)
        for document in (stripped, polished):
            self.assertEqual(document.paragraphs[0].text, 'Przed obrazem.')
            self.assertEqual(len(document.inline_shapes), 0)


def saved_png():
    from .support import png
    return png(4, 4).getvalue()


def with_part_image(source, part, rid):
    """Dodaje do części (bez własnych relacji) obraz word/media/bullet.png pod identyfikatorem rid."""
    folder, name = posixpath.split(part)
    output = BytesIO()
    with ZipFile(source) as archive, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for item in archive.infolist():
            data = archive.read(item.filename)
            if item.filename == '[Content_Types].xml' and b'Extension="png"' not in data:
                data = data.replace(b'<Default ', b'<Default Extension="png" ContentType="image/png"/><Default ', 1)
            target.writestr(item, data)
        target.writestr(posixpath.join(folder, '_rels', name + '.rels'), (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            'Target="media/bullet.png"/></Relationships>'))
        target.writestr('word/media/bullet.png', saved_png())
    output.seek(0)
    return output


@tag('docx')
class ForcePolishTests(SimpleTestCase):
    def languages(self, stream):
        found = {}
        with ZipFile(stream) as archive:
            for name in archive.namelist():
                if name.startswith('word/') and name.endswith('.xml'):
                    root = etree.fromstring(archive.read(name))
                    values = [node.get(qn('w:val')) for node in root.iter(qn('w:lang'), qn('w:themeFontLang'))]
                    if values:
                        found[name] = set(values)
        return found

    def test_every_language_mark_becomes_polish(self):
        document = Document()
        set_language(document.add_paragraph().add_run('Hello world.'), 'en-GB')
        set_language(document.styles['Heading 1'], 'de-DE')
        set_default_language(document, 'en-US')
        document.sections[0].header.paragraphs[0].text = 'Header'
        set_language(document.sections[0].header.paragraphs[0].runs[0], 'fr-FR')
        settings = document.settings.element
        settings.append(parse_xml(f'<w:themeFontLang {nsdecls("w")} w:val="en-US" w:eastAsia="ja-JP"/>'))
        source = with_footnote(document, 'Note')
        found = self.languages(force_polish(source))
        self.assertTrue({'word/document.xml', 'word/styles.xml', 'word/header1.xml', 'word/settings.xml'} <= set(found))
        self.assertEqual(set().union(*found.values()), {'pl-PL'})

    def test_missing_default_language_is_added(self):
        document = Document()
        document.add_paragraph('Tekst bez oznaczenia języka.')
        defaults = document.styles.element.find(qn('w:docDefaults'))
        document.styles.element.remove(defaults)
        result = force_polish(saved(document))
        styles = Document(result).styles.element
        lang = styles.find(qn('w:docDefaults') + '/' + qn('w:rPrDefault') + '/' + qn('w:rPr') + '/' + qn('w:lang'))
        self.assertEqual(lang.get(qn('w:val')), 'pl-PL')
        self.assertEqual(styles[0].tag, qn('w:docDefaults'))

    def test_language_is_placed_before_later_run_properties(self):
        document = Document()
        properties = document.styles.element.find(qn('w:docDefaults') + '/' + qn('w:rPrDefault') + '/' + qn('w:rPr'))
        for child in list(properties):
            properties.remove(child)
        properties.append(parse_xml(f'<w:sz {nsdecls("w")} w:val="24"/>'))
        properties.append(parse_xml(f'<w:eastAsianLayout {nsdecls("w")} w:vert="1"/>'))
        styles = Document(force_polish(saved(document))).styles.element
        properties = styles.find(qn('w:docDefaults') + '/' + qn('w:rPrDefault') + '/' + qn('w:rPr'))
        self.assertEqual([etree.QName(child).localname for child in properties], ['sz', 'lang', 'eastAsianLayout'])

    def test_polish_document_is_returned_unchanged(self):
        data = force_polish(saved(Document())).getvalue()
        self.assertEqual(force_polish(BytesIO(data)).getvalue(), data)


@tag('docx')
class WorkerImageTests(SimpleTestCase):
    """main() podprocesu: obrazy i język przed każdym programem, liczba obrazów w images.json."""

    def run_main(self, config, source):
        seen = {}

        def capture(name, result):
            def operation(document, **options):
                document.seek(0)
                seen[name] = document.read()
                return BytesIO(seen[name]) if result is None else result
            return operation

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'source.docx').write_bytes(source.getvalue())
            (directory / 'job.json').write_text(json.dumps({'formats': [], 'include_docx': True, 'title': 'T', **config}))
            with patch.object(sys, 'argv', ['worker', str(directory)]), \
                    patch('core.services.document_conversion_worker.prepare_docx', side_effect=capture('prepare', None)), \
                    patch('core.services.document_conversion_worker.inspect_docx', side_effect=capture('inspect', 0)), \
                    patch('core.services.document_conversion_worker.color_document', side_effect=capture('color', None)):
                code = worker.main()
            self.assertEqual(code, 0)
            seen['images'] = json.loads((directory / 'images.json').read_text())
        return seen

    def assertStripped(self, data):
        with ZipFile(BytesIO(data)) as archive:
            self.assertFalse([name for name in archive.namelist() if 'media/' in name])
            self.assertIn(b'w:val="pl-PL"', archive.read('word/styles.xml'))

    def test_every_program_gets_a_document_without_images(self):
        for config, step in (({'prepare': True, 'clean': True, 'cleaner_rules': ['spaces']}, 'prepare'),
                             ({'inspect': True}, 'inspect'),
                             ({'prepare': True, 'repetitions': {'window_size': 35}}, 'color')):
            with self.subTest(step=step):
                seen = self.run_main(config, docx_with_images())
                self.assertStripped(seen[step])
                self.assertEqual(seen['images'], {'removed_images': 6})

    def test_document_without_images_reports_zero(self):
        seen = self.run_main({'prepare': True}, BytesIO(docx_bytes()))
        self.assertEqual(seen['images'], {'removed_images': 0})

    def test_linked_images_are_unlinked_before_every_program_and_counted_apart(self):
        """Obrazy tylko połączone z plikami zewnętrznymi znikają razem z linkiem przed pracą programu i są liczone
        osobno (linked_images); obraz osadzony i połączony traci link, a potem znika jak każdy obraz."""
        seen = self.run_main({'prepare': True, 'clean': True, 'cleaner_rules': ['spaces']}, docx_with_linked_images())
        self.assertStripped(seen['prepare'])
        self.assertEqual(seen['images'], {'removed_images': 1, 'linked_images': 7})
        self.assertEqual(external_images(BytesIO(seen['prepare'])), [])

    def test_split_unlinks_images_and_keeps_embedded_ones(self):
        """Podział nie usuwa obrazów, ale i tu link do pliku zewnętrznego znika (liczba w split.json); obraz
        osadzony i połączony zostaje w pliku opowiadania jako osadzony."""
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'source.docx').write_bytes(docx_with_linked_images(heading=True).getvalue())
            (directory / 'job.json').write_text(json.dumps({'formats': [], 'split': True, 'title': 'T'}))
            with patch.object(sys, 'argv', ['worker', str(directory)]):
                self.assertEqual(worker.main(), 0)
            report = json.loads((directory / 'split.json').read_text())
            self.assertEqual(report['linked_images'], 7)
            with ZipFile(directory / 'document.zip') as archive:
                story = BytesIO(archive.read(report['files'][0]))
        self.assertEqual(external_images(story), [])
        with ZipFile(story) as archive:
            self.assertTrue([name for name in archive.namelist() if name.startswith('word/media/')])
            body = archive.read('word/document.xml').decode()
        self.assertIn('r:embed=', body)
        self.assertNotIn('r:link=', body)



@tag('docx')
class LinkedImageTests(SimpleTestCase):
    """Decyzja właściciela: obraz połączony z plikiem zewnętrznym nie blokuje pliku – usuwamy sam link."""

    def test_links_are_removed_and_only_linked_images_disappear(self):
        """Obrazy tylko połączone (rysunek wp:inline i wp:anchor, VML, pole tekstowe liczone raz, nagłówek, stopka,
        przypis) znikają; obraz osadzony i połączony zostaje bez r:link; hiperłącze zewnętrzne i tekst zostają,
        w pakiecie nie ma odwołań bez relacji."""
        source = docx_with_linked_images()
        self.assertEqual(len(external_images(source)), 5)  # treść (2), nagłówek, stopka, przypisy
        result, count = unlink_external_images(source)
        self.assertEqual(count, 7)
        self.assertEqual(external_images(result), [])
        self.assertEqual(package_problems(result), [])
        with ZipFile(result) as archive:
            parts = {name: archive.read(name).decode() for name in archive.namelist() if name.endswith(('.xml', '.rels'))}
        result.seek(0)
        self.assertIn('https://example.test/', parts['word/_rels/document.xml.rels'])
        self.assertFalse([name for name, xml in parts.items() if 'r:link=' in xml or 'o:href=' in xml])
        self.assertEqual(parts['word/document.xml'].count('<a:blip '), 1)
        self.assertEqual(parts['word/document.xml'].count('<pic:pic>'), 1)
        for text in ('Pole z obrazem', 'strona'):
            self.assertIn(text, parts['word/document.xml'])
        self.assertIn('Przypis z obrazem', parts['word/footnotes.xml'])
        document = Document(result)
        self.assertEqual([paragraph.text for paragraph in document.paragraphs],
                         ['Przed obrazami.', '', 'Obrazy: strona', ''])
        self.assertEqual(document.sections[0].header.paragraphs[0].text, 'Nagłówek ')
        self.assertEqual(document.sections[0].footer.paragraphs[0].text, 'Stopka ')
        # Obraz osadzony usuwa dopiero strip_images (liczony jak każdy obraz).
        self.assertEqual(strip_images(result)[1], 1)
        # Dokument bez obrazów połączonych wraca bez zmian.
        data = docx_with_images().getvalue()
        unchanged, count = unlink_external_images(data)
        self.assertEqual((unchanged.getvalue(), count), (data, 0))

    def test_note_names_the_number_of_linked_images(self):
        for count, expected in ((0, ''), (1, 'Usunięto 1 obraz połączony z plikiem zewnętrznym'),
                                (3, 'Usunięto 3 obrazy połączone z plikami zewnętrznymi'),
                                (5, 'Usunięto 5 obrazów połączonych z plikami zewnętrznymi'),
                                (12, 'Usunięto 12 obrazów połączonych z plikami zewnętrznymi'),
                                (22, 'Usunięto 22 obrazy połączone z plikami zewnętrznymi')):
            with self.subTest(count=count):
                note = linked_images_note(count)
                self.assertEqual(note.split(' – ')[0], expected)
                self.assertEqual(bool(note), bool(count))


IMAGE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image'
THUMBNAIL = 'http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail'
HYPERLINK = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink'


def repack(data, replace=None, extra=None):
    """Kopia pakietu z podmienionymi częściami {nazwa: funkcja(bajty)} i dopisanymi {nazwa: bajty}."""
    output = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(output, 'w') as target:
        for item in source.infolist():
            target.writestr(item, (replace or {}).get(item.filename, lambda value: value)(source.read(item)))
        for name, value in (extra or {}).items():
            target.writestr(name, value)
    return output.getvalue()


def relationship(rid, target, kind=IMAGE, mode=''):
    attributes = f' TargetMode="{mode}"' if mode else ''
    return f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"{attributes}/>'.encode()


def add_relationships(*items):
    return lambda data: data.replace(b'</Relationships>', b''.join(items) + b'</Relationships>')


def picture_document():
    """DOCX z jednym obrazem w treści; zwraca (bajty, identyfikator relacji obrazu)."""
    document = Document()
    document.add_picture(png(20, 10))
    rid = next(rid for rid, rel in document.part.rels.items() if rel.reltype == IMAGE)
    return document, rid


def read(data, name):
    with ZipFile(BytesIO(data)) as archive:
        return archive.read(name).decode()


@tag('docx')
class UnusualImageMarkupTests(SimpleTestCase):
    def test_every_kind_of_image_is_removed_and_text_survives(self):
        from docx.oxml import parse_xml
        document, rid = picture_document()
        document.element.body.remove(document.paragraphs[0]._p)
        picture = PICTURE_DRAWING.format(id=11, rid=rid)
        paragraph = document.add_paragraph()
        fragments = (
            # Tekst „ogona” po rysunku (nietypowy XML): zostaje przy poprzedniku albo w rodzicu.
            f'<w:r {IMAGE_NAMESPACES}><w:t>Przed</w:t>{picture}ogon1</w:r>',
            f'<w:r {IMAGE_NAMESPACES}>{picture}ogon2</w:r>',
            # Grupa kształtów: obraz w grupie i kształt z obrazkowym wypełnieniem.
            f'<w:r {IMAGE_NAMESPACES} xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup">'
            '<w:drawing><wp:inline><wp:docPr id="12" name="Grupa"/><a:graphic><a:graphicData '
            'uri="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"><wpg:wgp>'
            f'<pic:pic><pic:blipFill><a:blip r:embed="{rid}"/></pic:blipFill></pic:pic>'
            f'<wps:wsp><wps:spPr><a:blipFill><a:blip r:embed="{rid}"/></a:blipFill></wps:spPr></wps:wsp>'
            '</wpg:wgp></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>',
            # Pole tekstowe VML z obrazkowym wypełnieniem: tekst zostaje, znika wypełnienie.
            f'<w:r {IMAGE_NAMESPACES}><w:pict><v:shape><v:fill r:id="{rid}" type="frame"/><v:textbox>'
            '<w:txbxContent><w:p><w:r><w:t>Tekst w polu VML</w:t></w:r></w:p></w:txbxContent></v:textbox>'
            '</v:shape></w:pict></w:r>',
            # Treść alternatywna z samym obrazem w obu wersjach znika w całości (liczona raz).
            f'<w:r {IMAGE_NAMESPACES}><mc:AlternateContent><mc:Choice Requires="wps">{picture}</mc:Choice>'
            f'<mc:Fallback><w:pict><v:shape><v:imagedata r:id="{rid}"/></v:shape></w:pict></mc:Fallback>'
            '</mc:AlternateContent></w:r>',
        )
        for fragment in fragments:
            paragraph._p.append(parse_xml(fragment))
        # Tło strony z obrazem (Projektowanie → Kolor strony → Efekty wypełnienia).
        document.element.insert(0, parse_xml(
            f'<w:background {IMAGE_NAMESPACES} w:color="FFFFFF"><v:background><v:fill r:id="{rid}" type="frame"/>'
            '</v:background></w:background>'))
        result, removed = strip_images(saved(document))
        self.assertEqual(removed, 7)
        body = read(result.getvalue(), 'word/document.xml')
        for gone in ('pic:pic', 'a:blip', 'v:fill', 'imagedata', 'AlternateContent', 'w:background'):
            self.assertNotIn(gone, body)
        self.assertIn('<w:t>Przed</w:t>ogon1</w:r>', body)
        self.assertIn('>ogon2</w:r>', body)
        self.assertIn('Tekst w polu VML', body)
        # Kształt z grupy zostaje, ale bez obrazkowego wypełnienia.
        self.assertIn('<wps:spPr><a:noFill/></wps:spPr>', body)
        with ZipFile(result) as archive:
            self.assertFalse([name for name in archive.namelist() if name.startswith('word/media/')])


@tag('docx')
class PackageRelationshipTests(SimpleTestCase):
    def test_relationships_and_parts_of_removed_images(self):
        from docx.oxml import parse_xml
        document, rid = picture_document()
        # Tło strony bez obrazu (sam kolor) zostaje.
        document.element.insert(0, parse_xml(f'<w:background {IMAGE_NAMESPACES} w:color="FF0000"/>'))
        paragraph = document.sections[0].header.paragraphs[0]
        paragraph.text = 'Nagłówek bez obrazu'
        # Relacja obrazu, do której nagłówek wciąż odsyła (tu z odnośnika), zostaje.
        paragraph._p.append(parse_xml(f'<w:hyperlink {IMAGE_NAMESPACES} r:id="rIdKeep"><w:r><w:t>logo</w:t></w:r>'
                                      '</w:hyperlink>'))
        data = saved(document).getvalue()
        with ZipFile(BytesIO(data)) as archive:
            media = next(name for name in archive.namelist() if name.startswith('word/media/'))
            header = next(name for name in archive.namelist() if re.fullmatch(r'word/header\d+\.xml', name))
        header_rels = header.replace('word/', 'word/_rels/') + '.rels'
        data = repack(data, replace={
            # Adres bezwzględny obrazu treści, nieużywany obraz zewnętrzny i nadmiarowy wpis typu.
            'word/_rels/document.xml.rels': lambda value: add_relationships(
                relationship('rIdLink', 'https://example.test/x.png', mode='External'),
                relationship('rIdWeb', 'https://example.test/', HYPERLINK, 'External'))(
                value.replace(f'Target="{media[5:]}"'.encode(), f'Target="/{media}"'.encode())),
            '[Content_Types].xml': lambda value: value.replace(
                b'</Types>', f'<Override PartName="/{media}" ContentType="image/png"/></Types>'.encode()),
            # Miniatura pakietu (zewnętrzna) też znika, gdy usunięto obrazy.
            '_rels/.rels': add_relationships(relationship('rIdThumb', 'https://example.test/t.jpeg', THUMBNAIL,
                                                          'External')),
        }, extra={
            # Relacja obrazu, do którego nagłówek nie odsyła.
            header_rels: b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
                         b'2006/relationships">' + relationship('rIdLost', 'media/lost.png')
                         + relationship('rIdKeep', 'media/logo.png') + b'</Relationships>',
            # Uszkodzone części i relacje: zostają bez zmian.
            'word/broken.xml': b'<w:drawing>bez zamkniecia',
            'word/_rels/broken.xml.rels': b'<Relationships>' + relationship('rIdB', 'media/broken.png'),
            'word/damaged.xml': b'<w:pict><<',
            'word/_rels/damaged.xml.rels': b'<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                                           b'openxmlformats.org/package/2006/relationships">'
                                           + relationship('rIdD', 'media/damaged.png') + b'</Relationships>',
            # Relacje części, której nie ma w pakiecie.
            'word/_rels/missing.xml.rels': b'<Relationships/>',
        })
        result, removed = strip_images(data)  # bajty, nie strumień
        self.assertEqual(removed, 1)
        output = result.getvalue()
        with ZipFile(BytesIO(output)) as archive:
            names = archive.namelist()
            self.assertNotIn(media, names)
            for kept in ('word/broken.xml', 'word/damaged.xml', 'word/_rels/missing.xml.rels'):
                self.assertIn(kept, names)
            self.assertEqual(archive.read('word/_rels/broken.xml.rels'), data_part(data, 'word/_rels/broken.xml.rels'))
        document_rels = read(output, 'word/_rels/document.xml.rels')
        self.assertNotIn(rid, document_rels)
        self.assertNotIn('rIdLink', document_rels)
        self.assertNotIn('rIdThumb', read(output, '_rels/.rels'))
        self.assertIn('rIdWeb', document_rels)
        self.assertRegex(read(output, 'word/document.xml'), r'<w:background [^>]*w:color="FF0000"/>')
        self.assertNotIn('rIdLost', read(output, header_rels))
        self.assertIn('rIdKeep', read(output, header_rels))
        self.assertIn('rIdD', read(output, 'word/_rels/damaged.xml.rels'))
        types = etree.fromstring(read(output, '[Content_Types].xml').encode())
        self.assertFalse([entry for entry in types if entry.get('PartName') == f'/{media}'])

    def test_part_in_utf16_loses_its_images_and_becomes_polish(self):
        """Regresja: część XML zapisana w UTF-16 (z BOM) omijała skrót po bajtach ASCII – obraz zostawał
        w dokumencie wbrew zasadzie „bez obrazów”, a język tekstu nie był zmieniany na polski."""
        document, _ = picture_document()
        document.paragraphs[0].add_run('Tekst')._r.get_or_add_rPr().append(etree.fromstring(
            '<w:lang xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" w:val="en-US"/>'))
        data = repack(saved(document).getvalue(), replace={'word/document.xml': lambda value: value.decode(
            'utf-8').replace("encoding='UTF-8'", "encoding='UTF-16'").replace(
            'encoding="UTF-8"', 'encoding="UTF-16"').encode('utf-16')})
        self.assertTrue(data_part(data, 'word/document.xml').startswith(b'\xff\xfe'))
        stripped, removed = strip_images(data)
        self.assertEqual(removed, 1)
        with ZipFile(force_polish(stripped)) as archive:
            self.assertFalse([name for name in archive.namelist() if name.startswith('word/media/')])
            body = archive.read('word/document.xml').decode('utf-8')
        self.assertNotIn('<w:drawing', body)
        self.assertIn('<w:lang w:val="pl-PL"/>', body)
        self.assertEqual(Document(force_polish(stripped)).paragraphs[0].text, 'Tekst')

    def test_damaged_part_with_language_is_left_unchanged(self):
        data = repack(saved(Document()).getvalue(), extra={'word/broken.xml': b'<w:lang w:val="en-US"'})
        with ZipFile(force_polish(data)) as archive:
            self.assertEqual(archive.read('word/broken.xml'), b'<w:lang w:val="en-US"')
            self.assertIn(b'w:val="pl-PL"', archive.read('word/styles.xml'))


def data_part(data, name):
    with ZipFile(BytesIO(data)) as archive:
        return archive.read(name)
