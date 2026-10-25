"""Dziennik aktywności: przenoszenie kolejki do bazy (flush_activity), czyszczenie starych wpisów (prune_activity),
polskie etykiety wpisów i filtrowanie strony dziennika."""
import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import TestCase
from django.urls import get_resolver, resolve, reverse
from django.utils import timezone

from core.activity import LABELS, describe_request
from core.activity_spool import enqueue_activity
from core.activity_targets import present_activities, target_for_match
from core.management.commands.flush_activity import Command
from core.models import UserActivity
from fantazmaty.testing import create_member, create_superuser
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
        # Stała liczba zapytań na partię (konta, istniejące klucze, jeden zapis z czasem żądania), a nie kilka na wpis.
        with self.assertNumQueries(4):
            self.flush()
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

    def test_limit_leaves_the_rest_for_the_next_run(self):
        for i in range(3):
            enqueue_activity(**entry(path=f'/{i}'))
        out, _ = self.flush(limit=2)
        self.assertIn('Przeniesiono wpisów: 2', out)
        self.assertEqual(len(list(self.spool.glob('*.json'))), 1)

    def test_missing_spool_directory_is_an_empty_run(self):
        with self.settings(ACTIVITY_SPOOL_DIR=self.spool / 'brak'):
            out, _ = self.flush()
        self.assertIn('Przeniesiono wpisów: 0', out)

    def test_every_malformed_record_is_quarantined(self):
        enqueue_activity(**entry())
        valid = json.loads(next(self.spool.glob('*.json')).read_text(encoding='utf-8'))
        broken = {
            'extra field': {**valid, 'body': 'x'},
            'naive time': {**valid, 'created_at': '2026-01-01T12:00:00'},
            'key not matching name': {**valid, 'source_key': '0' * 32},
            'boolean account': {**valid, 'user_id': True},
            'negative account': {**valid, 'user_id': -1},
            'too long action': {**valid, 'action': 'x' * 256},
            'text field not a string': {**valid, 'target': 5},
            'invalid HTTP status': {**valid, 'status_code': 99},
        }
        for index, data in enumerate(broken.values()):
            key = f'{index:032x}'
            data = {**data, 'source_key': data['source_key'] if data['source_key'] != valid['source_key'] else key}
            (self.spool / f'{key}.json').write_text(json.dumps(data), encoding='utf-8')
        out, _ = self.flush()
        self.assertIn('Przeniesiono wpisów: 1', out)
        self.assertIn(f'Odłożono uszkodzonych: {len(broken)}', out)
        self.assertEqual(len(list((self.spool / 'quarantine').glob('*.json'))), len(broken))

    def test_entry_removed_meanwhile_is_skipped(self):
        """Wpis usunięty przez równoległe przeniesienie nie jest ani liczony, ani odkładany jako uszkodzony."""
        for error in (FileNotFoundError, ValueError):
            enqueue_activity(**entry())

            def vanish(path, error=error):
                path.unlink()
                raise error

            with self.subTest(error=error.__name__), \
                    patch('core.management.commands.flush_activity.validated_record', side_effect=vanish):
                out, err = self.flush()
                self.assertIn('Przeniesiono wpisów: 0. Odłożono uszkodzonych: 0. Do ponowienia: 0.', out)
                self.assertEqual(err, '')

    def test_unreadable_entry_stays_in_queue_and_fails_the_run(self):
        enqueue_activity(**entry())
        with patch('core.management.commands.flush_activity.validated_record', side_effect=PermissionError), \
                self.assertRaises(CommandError):
            self.flush()
        self.assertEqual(len(list(self.spool.glob('*.json'))), 1)

    def test_database_failure_retries_entries_one_by_one(self):
        """Jeden wpis, którego nie da się zapisać, nie blokuje pozostałych i zostaje w kolejce."""
        enqueue_activity(**entry(path='/dobry'))
        enqueue_activity(**entry(path='/zly'))
        original = Command.write_batch

        def flaky(command, records):
            if len(records) > 1 or records[0][1]['path'] == '/zly':
                raise DatabaseError('zajęte')
            return original(command, records)

        out, err = StringIO(), StringIO()
        with patch.object(Command, 'write_batch', flaky), self.assertRaises(CommandError):
            call_command('flush_activity', stdout=out, stderr=err)
        self.assertIn('Przeniesiono wpisów: 1', out.getvalue())
        self.assertIn('Do ponowienia: 1', out.getvalue())
        self.assertIn('pozostaje w kolejce', err.getvalue())
        self.assertEqual(UserActivity.objects.get().path, '/dobry')
        remaining = json.loads(next(self.spool.glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(remaining['path'], '/zly')


class PruneActivityTests(TestCase):
    """Czyszczenie dziennika usuwa tylko stare udane odwiedziny i zachowuje ostatnią aktywność każdej osoby."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user('aktywny', 'aktywny@example.test')
        cls.idle = User.objects.create_user('nieaktywny', 'nieaktywny@example.test')

        def row(user, action, days=200, **fields):
            data = {'user': user, 'actor': 'x', 'method': 'GET', 'action': action, 'path': '/', 'status_code': 200}
            created = UserActivity.objects.create(**{**data, **fields})
            UserActivity.objects.filter(pk=created.pk).update(created_at=timezone.now() - timedelta(days=days))

        row(cls.user, 'Świeży podgląd', days=0)
        row(cls.user, 'Stary podgląd')
        row(cls.user, 'Stary podgląd HEAD', method='HEAD')
        row(cls.user, 'Stary zapis', method='POST')
        row(cls.user, 'Stary błąd', status_code=404)
        row(cls.user, 'Logowanie')
        row(None, 'Anonimowy podgląd')
        # Jedyny wpis drugiej osoby to jej ostatnia znana aktywność – raport nieaktywności go potrzebuje.
        row(cls.idle, 'Ostatni podgląd')

    def prune(self, *args):
        out = StringIO()
        call_command('prune_activity', *args, stdout=out)
        return out.getvalue()

    def remaining(self):
        return set(UserActivity.objects.values_list('action', flat=True))

    def test_dry_run_counts_without_deleting(self):
        self.assertIn('Do usunięcia: 3', self.prune('--dry-run'))
        self.assertEqual(len(self.remaining()), 8)

    def test_only_old_successful_visits_are_removed(self):
        self.assertIn('Usunięto: 3', self.prune())
        self.assertEqual(self.remaining(), {'Świeży podgląd', 'Stary zapis', 'Stary błąd', 'Logowanie',
                                            'Ostatni podgląd'})
        self.assertIn('Usunięto: 0', self.prune('--days', '1'))

    def test_days_must_be_positive(self):
        with self.assertRaises(CommandError):
            self.prune('--days', '0')


class ActivityLabelsTests(TestCase):
    """Wpisy dziennika mają polskie etykiety i nazwy obiektów zamiast surowych nazw adresów."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('activityadmin')
        cls.person = Illustrator.objects.create(first_name='Anna', last_name='Rysuje')

    def test_all_named_routes_have_polish_labels_and_admin_uses_model_name(self):
        from core.auth_urls import urlpatterns as auth_patterns
        from core.urls import urlpatterns
        from illustrations.urls import urlpatterns as illustration_patterns

        for route in [*urlpatterns, *illustration_patterns, *auth_patterns]:
            if route.name:
                self.assertIn(route.name, LABELS)

        def route_names(patterns):
            for route in patterns:
                if hasattr(route, 'url_patterns'):
                    yield from route_names(route.url_patterns)
                elif route.name:
                    yield route.name
        # Bez etykiet dla adresów, których już nie ma (np. dawne save_review, discord_test).
        self.assertEqual(set(LABELS) - set(route_names(get_resolver().url_patterns)), set())
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


class ActivityPageFilterTests(TemporaryActivitySpoolMixin, TestCase):
    """Strona dziennika: tylko dla superużytkownika, filtry rodzaju i dat, błędny zakres dat bez wyników."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('activityfilter')
        today = timezone.now()
        for action, method, days in (('Wpis podglądu', 'GET', 0), ('Wpis formularza', 'POST', 0),
                                     ('Dawny wpis', 'GET', 10)):
            row = UserActivity.objects.create(actor='x', method=method, action=action, path='/', status_code=200)
            UserActivity.objects.filter(pk=row.pk).update(created_at=today - timedelta(days=days))

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def actions(self, **params):
        response = self.client.get(reverse('core:user_activity'), params)
        return response, {entry.action for entry in response.context['activities']}

    def test_page_and_menu_link_are_for_superusers_only(self):
        """Dziennik aktywności jest dla superużytkownika (sekcja System); koordynator nie ma linku ani dostępu."""
        url = reverse('core:user_activity')
        self.assertContains(self.client.get(url), 'Aktywność użytkowników')
        home = self.client.get(reverse('core:home')).content.decode()
        system = home.index('data-menu-section="system"')
        self.assertGreater(home.index(url, system), system)
        coordinator = create_member('activity-koord', 'Koordynator')
        self.client.force_login(coordinator)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertNotContains(self.client.get(reverse('core:home')), url)

    def test_method_and_date_filters(self):
        today = timezone.localdate()
        self.assertEqual(self.actions(method='POST')[1], {'Wpis formularza'})
        self.assertEqual(self.actions(date_from=today.isoformat())[1], {'Wpis podglądu', 'Wpis formularza'})
        self.assertEqual(self.actions(date_to=(today - timedelta(days=5)).isoformat())[1], {'Dawny wpis'})

    def test_reversed_date_range_is_rejected(self):
        today = timezone.localdate()
        response, actions = self.actions(date_from=today.isoformat(),
                                         date_to=(today - timedelta(days=1)).isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'nie może być późniejsza', status_code=400)
        self.assertEqual(actions, set())
