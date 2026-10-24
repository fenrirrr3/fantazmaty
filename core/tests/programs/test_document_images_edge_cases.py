"""Usuwanie obrazów w nietypowym zapisie OOXML (tło strony, grupy kształtów, pole tekstowe VML z wypełnieniem,
tekst „ogona” po rysunku, część XML w UTF-16) oraz relacje i części pakietu: adresy bezwzględne, zewnętrzne,
osierocone, uszkodzony XML, wpisy typów i miniatura pakietu."""
import re
from io import BytesIO
from zipfile import ZipFile

from django.test import SimpleTestCase
from docx import Document
from lxml import etree

from core.services.document_images import force_polish, strip_images

from .support import IMAGE_NAMESPACES, PICTURE_DRAWING, png, saved

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
