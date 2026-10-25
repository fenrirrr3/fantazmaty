"""Konwerter po stronie serwera: czekanie na wolny konwerter, zatrzymanie procesu, który już się zakończył,
za duży raport błędu, uszkodzone pliki pomocnicze wyniku (summary.json, images.json) oraz obrazy połączone
z plikami zewnętrznymi (przyjmowane, z uwagą dla użytkownika)."""
import json
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from django.test import SimpleTestCase

from core.services.document_converter import (
    LINKED_FILE_MESSAGE, ConversionError, conversion_slot, convert_document, run_converter, validate_resources,
)
from core.services.document_images import linked_images_note

from .support import IsolatedProgramDirsMixin, docx_with_linked_images, formatted_docx


class Process:
    """Proces roboczy: kod wyjścia albo (code=None) proces, który wciąż działa."""

    def __init__(self, code=None):
        self.code, self.pid, self.waited = code, 4242, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self, timeout=None):
        self.waited += 1
        if self.code is None and timeout is not None:
            raise subprocess.TimeoutExpired('worker', timeout)
        return self.code

    def poll(self):
        return self.code


class Stop(Exception):
    """Przerwanie zadania zgłoszone przez funkcję kontrolną (np. użytkownik anulował pracę)."""


class FakeMsvcrt:
    """Blokada plikowa Windows: pierwszy LK_NBLCK zajmuje bajt, kolejny zgłasza OSError do LK_UNLCK."""
    LK_NBLCK, LK_UNLCK = 'nblck', 'unlck'

    def __init__(self):
        self.locked, self.calls = False, []

    def locking(self, fd, mode, length):
        self.calls.append((mode, length))
        if mode == self.LK_NBLCK:
            if self.locked:
                raise OSError('zajęte')
            self.locked = True
        else:
            self.locked = False


class ConversionSlotTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def test_windows_lock_is_taken_once_and_released_after_the_slot(self):
        """Na Windows (msvcrt) zajęty bajt blokady oznacza zajęty konwerter; wyjście ze slotu zwalnia blokadę."""
        msvcrt = FakeMsvcrt()
        windows = type('WindowsOs', (), {'name': 'nt', '__getattr__': lambda self, name: getattr(os, name)})()
        with patch('core.services.document_converter.os', windows), patch.dict(sys.modules, {'msvcrt': msvcrt}):
            with conversion_slot():
                with self.assertRaisesMessage(ConversionError, 'przetwarza teraz inny dokument'):
                    with conversion_slot():
                        pass
            self.assertEqual(msvcrt.calls, [('nblck', 1), ('nblck', 1), ('unlck', 1)])
            self.assertFalse(msvcrt.locked)
            with conversion_slot():
                pass
        self.assertEqual(msvcrt.calls[-2:], [('nblck', 1), ('unlck', 1)])

    def test_slot_is_retried_every_second_without_a_wait_callback(self):
        with patch('core.services.document_converter._try_lock', side_effect=[False, False, True]) as lock, \
                patch('core.services.document_converter.time.sleep') as sleep:
            with conversion_slot(wait=60) as directory:
                self.assertTrue(directory.is_dir())
        self.assertEqual(lock.call_count, 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(1,), (1,)])


class RunConverterEdgeTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.directory = self.work_dir

    def start(self, process, control):
        with patch('core.services.document_converter.subprocess.Popen', return_value=process), \
                patch('core.services.document_converter.os.killpg', side_effect=ProcessLookupError) as kill:
            try:
                return run_converter(self.directory, 5, control=control)
            finally:
                self.kill = kill

    def test_time_used_by_the_control_function_counts_against_the_limit(self):
        """Gdy kontrola postępu zużyje cały czas, proces jest zatrzymywany (nawet jeśli już zniknął)."""
        process = Process()
        clock = iter([0, 100])
        with patch('core.services.document_converter.time.monotonic', side_effect=lambda: next(clock)), \
                self.assertRaisesMessage(ConversionError, 'limit 90 sekund'):
            self.start(process, lambda directory: None)
        self.kill.assert_called_once()
        self.assertEqual(process.waited, 1)

    def test_interrupted_job_kills_only_a_running_process(self):
        def stop(directory):
            raise Stop

        for code, killed in ((None, True), (0, False)):
            process = Process(code)
            with self.subTest(running=code is None), self.assertRaises(Stop):
                self.start(process, stop)
            self.assertEqual(self.kill.called, killed)
            self.assertEqual(process.waited, 1)

    def test_oversized_error_report_is_not_read(self):
        (self.directory / 'error.json').write_text(
            json.dumps({'error': 'DocumentInputError', 'public_code': 'tracked_changes', 'x': 'y' * 40000}))
        with patch('core.services.document_converter.subprocess.Popen', return_value=Process(3)), \
                self.assertLogs('core.services.document_converter', 'ERROR'), \
                self.assertRaisesMessage(ConversionError, '(start: ProcessTerminated)'):
            run_converter(self.directory, 5)


class DamagedResultFilesTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def test_unreadable_pdf_error_report_does_not_block_the_epub(self):
        """Raport błędu PDF, którego nie da się odczytać, nie zabiera gotowego EPUB (log pomijany)."""
        def worker(directory, timeout, **_):
            (directory / 'document.epub').write_bytes(b'PK epub')
            (directory / 'pdf_error.json').write_text('{"error": "ValueError"}', encoding='utf-8')

        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name == 'pdf_error.json':
                raise OSError('brak dostępu')
            return original(path, *args, **kwargs)

        with patch('core.services.document_converter.run_converter', side_effect=worker), \
                patch.object(Path, 'read_text', read_text):
            result, extension, _ = convert_document(BytesIO(formatted_docx()), ['pdf', 'epub'])
        with result:
            self.assertEqual((extension, result.read()), ('epub', b'PK epub'))

    def test_unexpected_summary_and_image_counts_are_ignored(self):
        """Granica zaufania: konwerter przepuszcza z summary.json i images.json tylko dodatnie liczby całkowite
        i poprawne zamiany słownikowe – program_jobs nie sprawdza ich ponownie."""
        cases = {
            'lista zamiast słownika': ('[1, 2]', '[3]'),
            'uszkodzony JSON': ('{"spaces": 2}', '{uszkodzony'),
            'liczba jako tekst': ('{"spaces": 2, "replacements": "x"}', '{"removed_images": "3"}'),
            'błędne zamiany': ('{"spaces": 2, "zero": 0, "flaga": true, "replacements": ['
                               '{"source": "a", "target": "b", "count": 0}, {"source": "a", "target": "b", "count": true},'
                               '{"source": "a", "target": "b", "count": 2}]}', '{}'),
        }
        expected = {'lista zamiast słownika': None, 'uszkodzony JSON': {'spaces': 2},
                    'liczba jako tekst': {'spaces': 2},
                    'błędne zamiany': {'spaces': 2, 'replacements': [{'source': 'a', 'target': 'b', 'count': 2}]}}
        for label, (summary, images) in cases.items():
            def worker(directory, timeout, summary=summary, images=images, **_):
                (directory / 'document.epub').write_bytes(b'PK epub')
                (directory / 'summary.json').write_text(summary, encoding='utf-8')
                (directory / 'images.json').write_text(images, encoding='utf-8')

            with self.subTest(label), patch('core.services.document_converter.run_converter', side_effect=worker):
                result, extension, _ = convert_document(BytesIO(formatted_docx()), ['epub'], cleaner_summary=True)
                result.close()
            self.assertEqual(extension, 'epub')
            self.assertEqual(result.cleaner_summary, expected[label])
            self.assertEqual(result.removed_images, 0)


class LinkedImageValidationTests(IsolatedProgramDirsMixin, SimpleTestCase):
    """Decyzja właściciela: obraz połączony z plikiem zewnętrznym nie blokuje pliku – proces roboczy usuwa link."""

    def test_linked_images_pass_validation_and_other_linked_files_do_not(self):
        """Regresja: plik z obrazem połączonym był odrzucany („Osadź go w DOCX”). Inne podłączone pliki
        (np. połączony obiekt OLE) nadal są odrzucane – CMS nigdy nie pobiera zasobów z zewnątrz."""
        source = docx_with_linked_images()
        validate_resources(source)
        self.assertEqual(source.tell(), 0)
        linked_object = BytesIO()
        with ZipFile(source) as archive, ZipFile(linked_object, 'w') as target:
            for item in archive.infolist():
                data = archive.read(item.filename)
                if item.filename == 'word/_rels/document.xml.rels':
                    data = data.replace(b'relationships/hyperlink" Target="https://example.test/"',
                                        b'relationships/oleObject" Target="file:///C:/Arkusz.xlsx"')
                target.writestr(item, data)
        with self.assertRaisesMessage(ConversionError, LINKED_FILE_MESSAGE):
            validate_resources(linked_object)

    def test_linked_images_get_a_note_before_other_warnings(self):
        """Liczba obrazów tylko połączonych (images.json → linked_images) daje osobną uwagę na początku listy;
        liczba usuniętych obrazów osadzonych zostaje w removed_images."""
        for label, images, expected in (
                ('obrazy połączone', {'removed_images': 1, 'linked_images': 2}, [linked_images_note(2), 'Uwaga']),
                ('bez obrazów połączonych', {'removed_images': 1}, ['Uwaga'])):
            def worker(directory, timeout, images=images, **_):
                (directory / 'document.epub').write_bytes(b'PK epub')
                (directory / 'warnings.json').write_text(json.dumps(['Uwaga']), encoding='utf-8')
                (directory / 'images.json').write_text(json.dumps(images), encoding='utf-8')

            with self.subTest(label), patch('core.services.document_converter.run_converter', side_effect=worker):
                result, _, _ = convert_document(BytesIO(formatted_docx()), ['epub'])
                result.close()
                self.assertEqual(result.conversion_warnings, expected)
                self.assertEqual(result.removed_images, 1)
