"""Luki pokrycia infrastruktury: limity logowania, dziennik aktywności, podgląd użytkownika, wersje edycji."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import include, path, reverse
from django.urls.resolvers import URLResolver
from django.utils import timezone
from django.utils.crypto import salted_hmac

from core import auth_throttle
from core.account_identity import validate_account_email
from core.activity_spool import enqueue_activity
from core.activity_targets import object_name, present_activities
from core.admin_urls import polish_admin_patterns
from core.edit_versions import version_of
from core.models import AuthenticationAttempt, UserActivity
from core.user_preview import SESSION_KEY, UserPreviewMiddleware
from fantazmaty.testing import create_member, create_superuser

from .support import TemporaryActivitySpoolMixin

BAD_LOGIN = {'username': 'nikt@example.test', 'password': 'zle'}


def rate_key(value):
    return salted_hmac('cms-auth-rate', value, algorithm='sha256').hexdigest()


class LoginThrottleGapTests(TestCase):
    @override_settings(AUTH_THROTTLE_ENABLED=False)
    def test_disabled_throttle_records_nothing(self):
        for _ in range(12):
            self.assertEqual(self.client.post(reverse('login'), BAD_LOGIN).status_code, 200)
        self.assertFalse(AuthenticationAttempt.objects.exists())

    @override_settings(AUTH_THROTTLE_CLIENT_IP_HEADER='HTTP_X_REAL_IP')
    def test_invalid_trusted_header_falls_back_to_connection_address(self):
        self.client.post(reverse('login'), BAD_LOGIN, REMOTE_ADDR='192.0.2.9', HTTP_X_REAL_IP='to-nie-adres')
        self.assertTrue(AuthenticationAttempt.objects.filter(key=rate_key('login:ip:192.0.2.9')).exists())

    def test_request_without_identity_counts_only_address_limits(self):
        self.client.post(reverse('login'), {'password': 'zle'})
        self.assertEqual(AuthenticationAttempt.objects.count(), 2)
        self.assertFalse(AuthenticationAttempt.objects.filter(
            key=rate_key('login:account:')).exists())

    def test_expired_counter_not_yet_purged_starts_again(self):
        """Licznik, który wygasa dokładnie teraz, nie blokuje i liczy próby od nowa."""
        now = timezone.now()
        key = rate_key('login:ip:127.0.0.1')
        AuthenticationAttempt.objects.create(key=key, attempts=100, expires_at=now)
        with patch('core.auth_throttle.timezone.now', return_value=now):
            self.assertEqual(self.client.post(reverse('login'), BAD_LOGIN).status_code, 200)
        self.assertEqual(AuthenticationAttempt.objects.get(key=key).attempts, 1)

    def test_account_ceiling_without_password_still_blocks(self):
        with patch.object(auth_throttle, 'ACCOUNT_LOGIN_LIMIT', 1):
            data = {'username': 'ofiara@example.test', 'password': ''}
            self.assertEqual(self.client.post(reverse('login'), data, REMOTE_ADDR='192.0.2.1').status_code, 200)
            self.assertEqual(self.client.post(reverse('login'), data, REMOTE_ADDR='192.0.2.2').status_code, 429)


class ActivityLoggingFailureTests(TemporaryActivitySpoolMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('dziennik-admin')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_logging_failures_never_break_the_page(self):
        """Błąd opisu lub zapisu aktywności trafia do logu, a użytkownik dostaje zwykłą odpowiedź."""
        url = reverse('core:user_preview')
        cases = {
            'opis celu': ('core.activity_targets.target_for_match', 'POST', 'opisać aktywności'),
            'zapis do kolejki': ('core.activity_spool.enqueue_activity', 'GET', 'zapisać aktywności'),
        }
        for name, (target, method, message) in cases.items():
            with self.subTest(name), patch(target, side_effect=RuntimeError('awaria')), \
                    self.assertLogs('core.activity', 'ERROR') as logs:
                response = self.client.post(url, {}) if method == 'POST' else self.client.get(url)
                self.assertIn(response.status_code, (200, 302))
                self.assertIn(message, logs.output[0])

    def test_corrupted_visit_timestamp_records_the_visit_again(self):
        session = self.client.session
        session['_activity_visit'] = {'user': self.admin.pk, 'at': 'zepsute'}
        session.save()
        with patch('core.activity_spool.enqueue_activity') as queue:
            self.client.get(reverse('core:home'))
        queue.assert_called_once()
        self.assertIsInstance(self.client.session['_activity_visit']['at'], float)

    def test_spool_removes_temporary_file_when_rename_fails(self):
        with patch('core.activity_spool.os.replace', side_effect=OSError('pełny dysk')):
            with self.assertRaises(OSError):
                enqueue_activity(user_id=None, actor='a', method='GET', action='Pulpit', target='', path='/',
                                 status_code=200)
        from core.activity_spool import spool_directory
        self.assertEqual(list(spool_directory().iterdir()), [])


class ActivityPresentationTests(SimpleTestCase):
    def test_unknown_path_keeps_stored_action_and_target(self):
        """Wpis z adresem, którego już nie ma, pokazuje działanie i obiekt tak, jak je zapisano
        (dawne nazwy adresów nie są tłumaczone na etykiety)."""
        for action in ('save review', 'discord_test', 'review list', 'Coś innego'):
            with self.subTest(action=action):
                entry = UserActivity(action=action, target='user_id: #5', path='/usunieta-strona/', method='GET')
                present_activities([entry])
                self.assertEqual((entry.display_action, entry.display_target), (action, 'user_id: #5'))

    def test_object_name_stops_after_three_levels_of_relations(self):
        def node(**fields):
            return SimpleNamespace(_meta=object(), **fields)
        deep = node(text=node(review=node(anthology=node(title='Za głęboko'))))
        shallow = node(text=node(review=node(title='Recenzja')))
        self.assertEqual(object_name(deep), '')
        self.assertEqual(object_name(shallow), 'Recenzja')


class UserPreviewAsyncTests(TestCase):
    def test_async_user_lookup_returns_previewed_account(self):
        admin = create_superuser('podglad-admin')
        member = create_member('podgladany', 'Redaktor')
        request = RequestFactory().get('/')
        request.user = admin
        request.session = SessionStore()
        request.session[SESSION_KEY] = {'actor': admin.pk, 'target': member.pk}
        seen = {}

        def view(request):
            seen['user'] = asyncio.run(request.auser())
            return HttpResponse('ok')

        response = UserPreviewMiddleware(view)(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen['user'], member)
        self.assertEqual(request.preview_actor, admin)


class SmallInfrastructureGapTests(TestCase):
    def test_account_email_longer_than_field_is_refused(self):
        long_email = 'a' * 64 + '@' + ('b' * 63 + '.') * 3 + 'com'
        with self.assertRaisesMessage(ValidationError, 'Adres e-mail jest zbyt długi.'):
            validate_account_email(long_email)

    def test_permission_granted_from_permission_side_bumps_account_version(self):
        user = get_user_model().objects.create_user('uprawnienia')
        before = version_of(user)
        Permission.objects.get(codename='view_user').user_set.add(user)
        self.assertGreater(version_of(user), before)

    def test_polish_admin_app_list_and_nested_routes(self):
        """Sekcja panelu ma polski adres; zagnieżdżone trasy są tłumaczone, a dawne angielskie nie istnieją."""
        admin = create_superuser('panel-admin')
        self.client.force_login(admin)
        response = self.client.get(reverse('admin:app_list', args=['authors']))
        self.assertEqual(response.wsgi_request.path, '/panel/autorzy/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['app_label'], 'authors')

        def view(request):
            return HttpResponse()

        patterns = polish_admin_patterns([path('authors/', include([path('author/', include([
            path('<int:pk>/', view, name='inner')]))]))])
        self.assertEqual([str(pattern.pattern) for pattern in patterns], ['autorzy/'])
        nested = patterns[0].url_patterns[0]
        self.assertIsInstance(nested, URLResolver)
        self.assertEqual(nested.url_patterns[0].name, 'inner')
        self.assertEqual(patterns[0].resolve('autorzy/autor/5/').kwargs, {'pk': 5})
        self.assertEqual(self.client.get('/panel/authors/').status_code, 404)

