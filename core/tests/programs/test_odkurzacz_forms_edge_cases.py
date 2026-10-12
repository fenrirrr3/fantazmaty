"""Formularze programów: zapamiętane ustawienia złego typu i pliki DOCX zaszyfrowane albo bez treści dokumentu."""
from io import BytesIO
from zipfile import ZipFile, ZipInfo

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from core.odkurzacz_forms import OdkurzaczForm, RepetitionsForm

from .support import formatted_docx


def package(entries, encrypted=False):
    """Archiwum ZIP z wpisami (nazwa, bajty); encrypted – bit szyfrowania w katalogu centralnym (jak ZipCrypto)."""
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        for name, data in entries:
            archive.writestr(ZipInfo(name), data)
    data = bytearray(stream.getvalue())
    if encrypted:
        central = data.rindex(b'PK\x01\x02')  # ostatni wpis katalogu centralnego
        data[central + 8] |= 1
    return bytes(data)


class StoredSettingsTests(SimpleTestCase):
    def test_text_settings_of_another_type_are_skipped(self):
        stored = {'ignored_words': 5, 'tracked_words': 'kot pies', 'color_palette': ['dark']}
        self.assertEqual(RepetitionsForm.initial_from_settings(stored), {'tracked_words': 'kot pies'})


class DocumentPackageTests(SimpleTestCase):
    def test_encrypted_or_incomplete_packages_are_refused(self):
        with ZipFile(BytesIO(formatted_docx())) as archive:
            parts = [(item.filename, archive.read(item)) for item in archive.infolist()]
        cases = {
            'zaszyfrowany wpis': (package(parts, encrypted=True), 'Dokument nie może być zaszyfrowany.'),
            'bez word/document.xml': (package([part for part in parts if part[0] != 'word/document.xml']),
                                      'Ten plik nie jest prawidłowym dokumentem DOCX.'),
        }
        for label, (data, message) in cases.items():
            with self.subTest(label):
                form = OdkurzaczForm(data={}, files={'document': SimpleUploadedFile('tekst.docx', data)})
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors['document'], [message])
