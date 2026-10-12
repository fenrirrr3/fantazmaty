"""Konwerter po stronie serwera: czekanie na wolny konwerter, zatrzymanie procesu, który już się zakończył,
za duży raport błędu i uszkodzone pliki pomocnicze wyniku (summary.json, images.json)."""
import json
import subprocess
from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase

from core.services.document_converter import ConversionError, conversion_slot, convert_document, run_converter

from .support import IsolatedProgramDirsMixin, formatted_docx


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


class ConversionSlotTests(IsolatedProgramDirsMixin, SimpleTestCase):
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
    def test_unexpected_summary_and_image_counts_are_ignored(self):
        cases = {
            'lista zamiast słownika': ('[1, 2]', '[3]'),
            'uszkodzony JSON': ('{"spaces": 2}', '{uszkodzony'),
            'liczba jako tekst': ('{"spaces": 2, "replacements": "x"}', '{"removed_images": "3"}'),
        }
        expected = {'lista zamiast słownika': None, 'uszkodzony JSON': {'spaces': 2},
                    'liczba jako tekst': {'spaces': 2}}
        for label, (summary, images) in cases.items():
            def worker(directory, timeout, summary=summary, images=images):
                (directory / 'document.epub').write_bytes(b'PK epub')
                (directory / 'summary.json').write_text(summary, encoding='utf-8')
                (directory / 'images.json').write_text(images, encoding='utf-8')

            with self.subTest(label), patch('core.services.document_converter.run_converter', side_effect=worker):
                result, extension, _ = convert_document(BytesIO(formatted_docx()), ['epub'], cleaner_summary=True)
                result.close()
            self.assertEqual(extension, 'epub')
            self.assertEqual(result.cleaner_summary, expected[label])
            self.assertEqual(result.removed_images, 0)
