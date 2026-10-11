"""Pomocniki testów programów: pliki DOCX w pamięci i izolowane katalogi robocze."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from PIL import Image

# Poprawne parametry formularza powtórzeń (prefiks dodaje formularz strony programów).
REPETITION_PARAMETERS = {
    'window_size': 35,
    'min_word_length': 4,
    'sentence_limit': 35,
    'paragraph_limit': 150,
}
REPETITION_FIELDS = {f'repetitions-{key}': value for key, value in REPETITION_PARAMETERS.items()}


def saved(document):
    """Dokument python-docx zapisany do strumienia ustawionego na początek."""
    stream = BytesIO()
    document.save(stream)
    stream.seek(0)
    return stream


def docx_bytes(text='Ala  ma kota...'):
    document = Document()
    document.add_paragraph(text)
    return saved(document).getvalue()


def formatted_docx():
    """DOCX z kursywą 17 pt wyrównaną do lewej i wyśrodkowanym akapitem."""
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run('Ala  ma kota.').italic = True
    paragraph.runs[0].font.size = Pt(17)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    document.add_paragraph('Środek').alignment = WD_ALIGN_PARAGRAPH.CENTER
    return saved(document).getvalue()


def sized_docx(size):
    """Poprawny DOCX o dokładnie zadanym rozmiarze w bajtach."""
    stream = BytesIO(formatted_docx())
    with ZipFile(stream, 'a', compression=ZIP_STORED) as archive:
        overhead = 30 + 46 + 2 * len('padding.bin')
        archive.writestr('padding.bin', b'0' * (size - len(stream.getvalue()) - overhead))
    assert len(stream.getvalue()) == size
    return SimpleUploadedFile('tekst.docx', stream.getvalue())


def png(width, height):
    """Jednobarwny obraz PNG o zadanych wymiarach (strumień ustawiony na początek)."""
    stream = BytesIO()
    Image.new('RGB', (width, height), 'red').save(stream, 'PNG')
    stream.seek(0)
    return stream


def upload(data=None, name='tekst.docx'):
    return SimpleUploadedFile(name, docx_bytes() if data is None else data)


def downloaded(response):
    """Treść odpowiedzi strumieniowej (klient testowy sam ją zamyka)."""
    return b''.join(response.streaming_content)


def downloaded_document(response):
    return Document(BytesIO(downloaded(response)))


def typography_sample():
    """Akapit z kursywą, pogrubieniem i własnymi odstępami, tytuł wyśrodkowany stylem, akapit do prawej i tabela."""
    doc = Document()
    p = doc.add_paragraph()
    p.add_run('Kursywa ').italic = True
    p.add_run('pogrubienie').bold = True
    p.paragraph_format.space_after = Pt(20)
    p.paragraph_format.line_spacing = 2
    doc.add_paragraph('Środek', style='Title')
    doc.styles['Title'].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph('Prawo').alignment = WD_ALIGN_PARAGRAPH.RIGHT
    doc.add_table(rows=1, cols=1).cell(0, 0).text = 'Tabela'
    return saved(doc)


FOOTNOTES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
    '<w:footnote w:id="1"><w:p><w:r><w:t xml:space="preserve">{}</w:t></w:r></w:p></w:footnote>'
    '</w:footnotes>'
)


def with_footnote(document, text):
    """DOCX z przypisem dolnym (python-docx nie umie ich tworzyć)."""
    raw = saved(document)
    output = BytesIO()
    with ZipFile(raw) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == '[Content_Types].xml':
                data = data.replace(b'</Types>', b'<Override PartName="/word/footnotes.xml" ContentType="application/'
                                    b'vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>')
            if item.filename == 'word/_rels/document.xml.rels':
                data = data.replace(b'</Relationships>', b'<Relationship Id="rIdFoot" Type="http://schemas.openxmlformats.org/'
                                    b'officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/></Relationships>')
            target.writestr(item, data)
        target.writestr('word/footnotes.xml', FOOTNOTES.format(text))
    output.seek(0)
    return output


def footnote_text(stream):
    with ZipFile(stream) as archive:
        return archive.read('word/footnotes.xml').decode()


def whole_document():
    """Akapit, tabela, nagłówek i przypis – z podwójnymi spacjami i powtórzeniem „lampa”."""
    document = Document()
    document.add_paragraph('Akapit  główny.')
    document.add_table(rows=1, cols=1).cell(0, 0).text = 'Lampa świeciła,  lampa gasła.'
    document.sections[0].header.paragraphs[0].text = 'Nagłówek  strony'
    return with_footnote(document, 'Przypis  z lampą i lampą.')


class IsolatedProgramDirsMixin:
    """Zadania programów i dziennik aktywności zapisują pliki w prywatnym katalogu testu."""

    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work_dir = Path(temporary.name)
        override = self.settings(
            DOCUMENT_CONVERSION_DIR=self.work_dir / 'convert',
            ACTIVITY_SPOOL_DIR=self.work_dir / 'activity',
        )
        override.enable()
        self.addCleanup(override.disable)
