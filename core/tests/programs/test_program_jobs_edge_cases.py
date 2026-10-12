"""Zadania programów: nadzorca zadania uruchamiany jako skrypt, sprzątanie przy błędach systemu plików,
czekanie na blokadę tworzenia, kolejka z zadaniem, które właśnie zniknęło, i rzadkie znaki życia w kolejce."""
import io
import json
import os
import runpy
import time
from contextlib import redirect_stderr
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from core.services import program_jobs as jobs

from .support import IsolatedProgramDirsMixin

WORKER = Path(jobs.__file__).with_name('program_job_worker.py')


class ProgramJobWorkerScriptTests(IsolatedProgramDirsMixin, SimpleTestCase):
    """program_job_worker.py tak, jak uruchamia go launch(): argument to folder zadania."""

    def setUp(self):
        super().setUp()
        self.folder = jobs.root() / ('f' * 32)
        self.folder.mkdir(parents=True)

    def run_worker(self, **execute):
        # Skrypt zmienia umask procesu i sys.path – w teście oba zostają nietknięte.
        with patch('sys.argv', ['program_job_worker.py', str(self.folder)]), patch('os.umask') as umask, \
                patch('sys.path', list(__import__('sys').path)), \
                patch('core.services.program_jobs.execute', **execute) as run, redirect_stderr(io.StringIO()) as err:
            runpy.run_path(str(WORKER), run_name='__main__')
        return run, umask, err.getvalue()

    def test_worker_runs_the_job_in_the_configured_directory(self):
        run, umask, _ = self.run_worker()
        run.assert_called_once_with(self.folder)
        umask.assert_called_once_with(0o077)
        self.assertEqual(Path(settings.DOCUMENT_CONVERSION_DIR), self.work_dir / 'convert')

    def test_startup_failure_is_recorded_without_details(self):
        with self.assertRaises(SystemExit) as stop:
            self.run_worker(side_effect=ImportError('PRYWATNA ścieżka'))
        self.assertEqual(stop.exception.code, 1)
        state = json.loads((self.folder / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['state'], 'error')
        self.assertIn('Nie można uruchomić programu', state['message'])
        self.assertNotIn('PRYWATNA', json.dumps(state))
        self.assertFalse((self.folder / 'state.tmp').exists())


class CleanupFailureTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def test_folder_that_cannot_be_removed_does_not_stop_the_cleanup(self):
        jobs.root().mkdir(parents=True)
        old = time.time() - jobs.TTL - 60
        expired = [jobs.root() / ('a' * 32), jobs.root() / ('b' * 32),
                   jobs.root().parent / 'document-1', jobs.root().parent / 'document-2']
        for folder in expired:
            folder.mkdir()
            os.utime(folder, (old, old))
        removed, real = [], jobs.shutil.rmtree

        def locked_first(path, *args, **kwargs):
            # Pierwszy folder każdego rodzaju jest zablokowany (np. otwarty przez inny proces).
            if path in (expired[0], expired[2]):
                raise PermissionError(path)
            removed.append(path)
            real(path, *args, **kwargs)

        with patch('core.services.program_jobs.shutil.rmtree', side_effect=locked_first):
            jobs.cleanup()
        self.assertEqual(removed, [expired[1], expired[3]])
        self.assertTrue(expired[0].exists() and expired[2].exists())


class CreationLockTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def test_creation_waits_until_the_lock_is_free(self):
        jobs.root().mkdir(parents=True)
        with patch('core.services.program_jobs._try_lock', side_effect=[False, False, True]), \
                patch('core.services.program_jobs.time.sleep') as sleep:
            with jobs.creation_lock(wait=5):
                pass
        self.assertEqual([call.args for call in sleep.call_args_list], [(0.1,), (0.1,)])


class QueueAndRemovalTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def test_job_that_disappeared_from_the_queue_counts_with_the_default_limit(self):
        """Zadanie przed nami zakończyło się między odczytem kolejki a odczytem jego ustawień."""
        from core.services.document_converter import TIME_LIMIT
        mine, gone = jobs.root() / ('1' * 32), jobs.root() / ('2' * 32)
        others = [(1, gone)] * 20
        with patch.object(jobs, 'active_jobs', return_value=[(1, mine), *others]):
            self.assertEqual(jobs.queue_wait(mine), 20 * (TIME_LIMIT + jobs.QUEUE_MARGIN))

    def test_only_job_folders_inside_the_job_root_are_removed(self):
        jobs.root().mkdir(parents=True)
        foreign = jobs.root() / 'nie-zadanie'
        outside = self.work_dir / ('c' * 32)
        for folder in (foreign, outside):
            folder.mkdir()
            jobs.remove(folder)
            self.assertTrue(folder.exists())


class QueuedHeartbeatTests(IsolatedProgramDirsMixin, SimpleTestCase):
    def test_waiting_in_the_queue_writes_the_state_at_most_every_two_seconds(self):
        folder = jobs.root() / ('a' * 32)
        folder.mkdir(parents=True)
        jobs.write_json(folder / 'request.json', {'user': 1, 'session': 's', 'name': 'Tekst.docx',
                                                  'action': 'convert', 'options': {}})
        (folder / 'source.docx').write_bytes(b'docx')
        states = []

        def converter(source, control, on_wait, **options):
            on_wait()  # tuż po starcie: bez nowego zapisu stanu
            states.append(jobs.read_json(folder / 'state.json')['stage'])
            return BytesIO(b'%PDF'), 'pdf', 'application/pdf'

        with patch.object(jobs, 'convert_document', side_effect=converter):
            jobs.execute(folder)
        self.assertEqual(states, ['Sprawdzanie dokumentu'])
        self.assertEqual(jobs.status(folder)['state'], 'done')
