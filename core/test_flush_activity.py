"""The activity spool is moved to the database in batches without losing records."""
import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from core.activity_spool import enqueue_activity
from core.models import UserActivity


def entry(**overrides):
    data = dict(user_id=None, actor='a@example.test', method='GET', action='Pulpit', target='', path='/', status_code=200)
    data.update(overrides)
    return data


class FlushActivityTests(TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        override = override_settings(ACTIVITY_SPOOL_DIR=self.directory)
        override.enable()
        self.addCleanup(override.disable)

    def flush(self, **options):
        out, err = StringIO(), StringIO()
        call_command('flush_activity', stdout=out, stderr=err, **options)
        return out.getvalue(), err.getvalue()

    def test_moves_entries_and_keeps_request_time(self):
        user = get_user_model().objects.create_user('u', 'u@example.test', 'x')
        enqueue_activity(**entry(user_id=user.pk, action='Zapis'))
        path = next(self.directory.glob('*.json'))
        data = json.loads(path.read_text(encoding='utf-8'))
        stamp = timezone.now() - timedelta(days=2)
        data['created_at'] = stamp.isoformat()
        path.write_text(json.dumps(data), encoding='utf-8')
        out, _ = self.flush()
        self.assertIn('Przeniesiono wpisów: 1', out)
        row = UserActivity.objects.get()
        self.assertEqual(row.user, user)
        self.assertEqual(row.action, 'Zapis')
        self.assertLess(abs((row.created_at - stamp).total_seconds()), 1)
        self.assertFalse(list(self.directory.glob('*.json')))

    def test_many_entries_use_few_queries(self):
        for i in range(30):
            enqueue_activity(**entry(path=f'/{i}'))
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            self.flush()
        # A constant number of queries per batch, not several per entry.
        self.assertLessEqual(len(queries), 8)
        self.assertEqual(UserActivity.objects.count(), 30)

    def test_deleted_user_is_kept_as_anonymous_entry(self):
        enqueue_activity(**entry(user_id=999999))
        self.flush()
        self.assertIsNone(UserActivity.objects.get().user_id)

    def test_corrupt_entry_is_quarantined_and_does_not_block_others(self):
        enqueue_activity(**entry())
        (self.directory / ('f' * 32 + '.json')).write_text('{"broken": true}', encoding='utf-8')
        out, err = self.flush()
        self.assertIn('Przeniesiono wpisów: 1', out)
        self.assertIn('Odłożono uszkodzony wpis', err)
        self.assertTrue((self.directory / 'quarantine' / ('f' * 32 + '.json')).exists())

    def test_already_imported_entry_is_not_duplicated(self):
        enqueue_activity(**entry())
        path = next(self.directory.glob('*.json'))
        copy = path.read_bytes()
        self.flush()
        path.write_bytes(copy)
        self.flush()
        self.assertEqual(UserActivity.objects.count(), 1)

    def test_rejects_non_positive_limit(self):
        with self.assertRaises(CommandError):
            self.flush(limit=0)
