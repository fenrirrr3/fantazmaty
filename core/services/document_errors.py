"""Stable, public error messages; never include uploaded document content.

Także rozpoznanie plików, które nie są zwykłym DOCX (foreign_format) – wspólne dla formularzy i konwertera.
"""
from io import BytesIO
from zipfile import BadZipFile, ZipFile

SAVE_AS_DOCX = 'Otwórz go w Wordzie lub LibreOffice i zapisz jako „Dokument programu Word (.docx)”.'
MESSAGES = {
    'cleaner_paragraph_limit': 'Jeden z akapitów przekracza 20 000 znaków. Podziel ten akapit na krótsze. Odkurzacz nie ogranicza łącznej liczby znaków dokumentu.',
    'analysis_limit': 'Dokument przekracza limit analizy. Podziel go na mniejsze części.',
    'tracked_changes': 'Zaakceptuj albo odrzuć śledzone zmiany w dokumencie przed analizą.',
    'unsupported_run': 'Dokument zawiera nieobsługiwaną strukturę tekstu. Zapisz nową kopię DOCX lub wyłącz oznaczanie powtórzeń.',
    'memory_limit': 'Przetwarzanie tego dokumentu wymaga zbyt dużo pamięci. Podziel go na mniejsze części.',
    'analysis_options': 'Parametry analizy przekraczają dozwolony zakres. Sprawdź ustawienia powtórzeń.',
    'unsupported_image': 'Dokument zawiera obraz w formacie, którego konwerter nie obsługuje (np. EMF/WMF z Worda). '
                         'Zapisz ten obraz jako PNG lub JPG i wstaw go ponownie.',
    'image_too_large': 'Jeden z obrazów jest za duży (ponad 20 MB albo ponad 20 mln pikseli). Zmniejsz go przed konwersją.',
    'document_too_large': 'Treść dokumentu jest za duża do przetworzenia (po rozpakowaniu ponad 15 MB tekstu i znaczników). '
                          'Podziel dokument na mniejsze części albo zapisz go ponownie w Wordzie bez zbędnego formatowania.',
    'too_many_images': 'Obrazy w dokumencie zajmują łącznie za dużo miejsca. Zmniejsz je albo podziel dokument.',
    # Inny format z rozszerzeniem .docx albo odmiana DOCX, której biblioteki nie czytają (foreign_format).
    'format_doc': 'To plik w starym formacie Worda (.doc), a nie DOCX. ' + SAVE_AS_DOCX,
    'format_encrypted': 'Dokument jest zabezpieczony hasłem. Usuń hasło (w Wordzie: Plik → Informacje → Chroń '
                        'dokument → Szyfruj przy użyciu hasła) i wyślij plik ponownie.',
    'format_rtf': 'To plik RTF, a nie DOCX. ' + SAVE_AS_DOCX,
    'format_odt': 'To plik OpenDocument (LibreOffice, OpenOffice), a nie DOCX. ' + SAVE_AS_DOCX,
    'format_word_xml': 'To dokument XML Worda 2003, a nie DOCX. ' + SAVE_AS_DOCX,
    'format_html': 'To strona HTML, a nie DOCX. ' + SAVE_AS_DOCX,
    'format_pages': 'To plik Apple Pages, a nie DOCX. Wyeksportuj go w Pages: Plik → Eksportuj do → Word.',
    'format_strict': 'Dokument zapisano jako „Dokument Strict Open XML”, którego programy nie odczytują. '
                     'Zapisz go w Wordzie jako zwykły „Dokument programu Word (.docx)”.',
    'format_template': 'To szablon Worda albo dokument z makrami (.dotx, .dotm, .docm), a nie zwykły DOCX. '
                       'Zapisz go w Wordzie jako „Dokument programu Word (.docx)”.',
}

OLE_SIGNATURE = bytes.fromhex('d0cf11e0a1b11ae1')
# Typy zawartości części głównej szablonów i dokumentów z makrami (python-docx odrzuca je jako „nie Word”).
TEMPLATE_TYPES = (b'wordprocessingml.template.main+xml', b'ms-word.document.macroEnabled.main+xml',
                  b'ms-word.template.macroEnabledTemplate.main+xml')
STRICT_RELATIONSHIP = b'purl.oclc.org/ooxml/officeDocument/relationships/officeDocument'


def foreign_format(data):
    """Kod komunikatu (MESSAGES) dla pliku, który nie jest zwykłym DOCX, albo None.

    Rozpoznaje pliki innych formatów z rozszerzeniem .docx (Word 97–2003, DOCX zaszyfrowany hasłem – oba to
    kontener OLE, RTF, ODT, XML Worda 2003, HTML, Apple Pages) oraz odmiany DOCX, których biblioteki nie
    czytają (Strict Open XML, szablon, dokument z makrami). None nie przesądza, że DOCX jest poprawny.
    """
    if data.startswith(OLE_SIGNATURE):
        return 'format_encrypted' if 'EncryptedPackage'.encode('utf-16-le') in data else 'format_doc'
    if not data.startswith(b'PK'):
        head = data[:2048].lstrip(b'\xef\xbb\xbf \t\r\n').lower()
        if head.startswith(b'{\\rtf'):
            return 'format_rtf'
        if b'<w:worddocument' in head or b'progid="word.document"' in head:
            return 'format_word_xml'
        if b'<office:document' in head:
            return 'format_odt'  # płaski zapis OpenDocument (.fodt)
        if head.startswith(b'<!doctype html') or b'<html' in head:
            return 'format_html'
        return None
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())

            def head(name, size=262144):
                if name not in names:
                    return b''
                with archive.open(name) as part:
                    return part.read(size)

            if head('mimetype', 100).startswith(b'application/vnd.oasis.opendocument'):
                return 'format_odt'
            if 'Index/Document.iwa' in names:
                return 'format_pages'
            if STRICT_RELATIONSHIP in head('_rels/.rels'):
                return 'format_strict'
            if any(kind in head('[Content_Types].xml') for kind in TEMPLATE_TYPES):
                return 'format_template'
    except (BadZipFile, OSError, EOFError, NotImplementedError, RuntimeError, ValueError):
        return None
    return None


class DocumentInputError(ValueError):
    def __init__(self, code):
        self.public_code = code
        super().__init__(MESSAGES[code])
