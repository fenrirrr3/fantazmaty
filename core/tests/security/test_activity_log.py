"""Dziennik aktywności: przenoszenie kolejki do bazy (flush_activity) i polskie etykiety wpisów."""
import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone

from core.activity import LABELS, describe_request
from core.activity_spool import enqueue_activity
from core.activity_targets import present_activities, target_for_match
from core.models import UserActivity
from illustrations.models import Illustrator

from .support import TemporaryActivitySpoolMixin


def entry(**overrides):
    data = dict(user_id=None, actor='a@example.test', method='GET', action='Pulpit', target='', path='/',
                status_code=200)
    data.update(overrides)
    return data


class FlushActivityTests(TemporaryActivitySpoolMixin, TestCase):
    """Kolejka aktywności trafia do bazy partiami, bez gubienia i dublowania wpisów."""

    def flush(self, **options):
        out, err = StringIO(), StringIO()
        call_command('flush_activity', stdout=out, stderr=err, **options)
        return out.getvalue(), err.getvalue()

    def test_moves_entries_and_keeps_request_time(self):
        user = get_user_model().objects.create_user('u', 'u@example.test', 'x')
        enqueue_activity(**entry(user_id=user.pk, action='Zapis'))
        path = next(self.spool.glob('*.json'))
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
        self.assertFalse(list(self.spool.glob('*.json')))

    def test_many_entries_use_few_queries(self):
        for i in range(30):
            enqueue_activity(**entry(path=f'/{i}'))
        with CaptureQueriesContext(connection) as queries:
            self.flush()
        # Stała liczba zapytań na partię, a nie kilka na każdy wpis.
        self.assertLessEqual(len(queries), 8)
        self.assertEqual(UserActivity.objects.count(), 30)

    def test_deleted_user_is_kept_as_anonymous_entry(self):
        enqueue_activity(**entry(user_id=999999))
        self.flush()
        self.assertIsNone(UserActivity.objects.get().user_id)

    def test_corrupt_entry_is_quarantined_and_does_not_block_others(self):
        enqueue_activity(**entry())
        (self.spool / ('f' * 32 + '.json')).write_text('{"broken": true}', encoding='utf-8')
        out, err = self.flush()
        self.assertIn('Przeniesiono wpisów: 1', out)
        self.assertIn('Odłożono uszkodzony wpis', err)
        self.assertTrue((self.spool / 'quarantine' / ('f' * 32 + '.json')).exists())

    def test_already_imported_entry_is_not_duplicated(self):
        enqueue_activity(**entry())
        path = next(self.spool.glob('*.json'))
        copy = path.read_bytes()
        self.flush()
        path.write_bytes(copy)
        self.flush()
        self.assertEqual(UserActivity.objects.count(), 1)

    def test_rejects_non_positive_limit(self):
        with self.assertRaises(CommandError):
            self.flush(limit=0)


class ActivityLabelsTests(TestCase):
    """Wpisy dziennika mają polskie etykiety i nazwy obiektów zamiast surowych nazw adresów."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('activityadmin', 'admin@example.test', 'test')
        cls.person = Illustrator.objects.create(first_name='Anna', last_name='Rysuje')

    def test_all_named_routes_have_polish_labels_and_admin_uses_model_name(self):
        from core.auth_urls import urlpatterns as auth_patterns
        from core.urls import urlpatterns
        from illustrations.urls import urlpatterns as illustration_patterns

        for route in [*urlpatterns, *illustration_patterns, *auth_patterns]:
            if route.name:
                self.assertIn(route.name, LABELS)
        self.assertEqual(
            describe_request(resolve(reverse('illustrations:illustrator_list')), 'GET')[0], 'Ilustratorzy')
        admin_change = resolve(reverse('admin:illustrations_illustrator_change', args=[self.person.pk]))
        self.assertIn('ilustrator', describe_request(admin_change, 'POST')[0].casefold())

    def test_old_rows_are_readable_searchable_and_object_names_escaped(self):
        UserActivity.objects.create(
            user=self.admin, actor='Admin', method='GET', action='illustrator list', target='',
            path=reverse('illustrations:illustrator_list'), status_code=200,
        )
        old = UserActivity.objects.create(
            user=self.admin, actor='Admin', method='POST', action='Próba / formularz: illustrator edit',
            target=f'#{self.person.pk}', path=reverse('illustrations:illustrator_edit', args=[self.person.pk]),
            status_code=200,
        )
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:user_activity'))
        self.assertContains(page, 'Anna Rysuje')
        self.assertContains(page, 'Ilustratorzy')
        self.assertNotContains(page, 'illustrator list')
        self.assertContains(self.client.get(reverse('core:user_activity'), {'q': 'Ilustratorzy'}), 'Ilustratorzy')
        old.target = 'Zachowana nazwa (#1)'
        self.person.delete()
        present_activities([old])
        self.assertEqual(old.display_target, old.target)

    def test_new_activity_snapshots_object_name_not_post_body(self):
        self.client.force_login(self.admin)
        with patch('core.activity_spool.enqueue_activity') as queue:
            self.client.get(reverse('illustrations:illustrator_edit', args=[self.person.pk]))
        self.assertIn('Anna Rysuje', queue.call_args.kwargs['target'])
        match = resolve(reverse('illustrations:illustrator_edit', args=[self.person.pk]))
        self.assertIn('Anna Rysuje', target_for_match(match))
