"""Logowanie: limity prób logowania i resetu hasła (throttle), logowanie adresem e-mail, logowanie do panelu admina,
reset hasła z linku w e-mailu i zmiana hasła w układzie CMS."""
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.templatetags.static import static
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from core import auth_throttle
from core.auth_backends import EmailBackend
from core.models import AuthenticationAttempt
from fantazmaty.testing import create_member

from .support import TemporaryActivitySpoolMixin

BAD_LOGIN = {'username': 'no@example.test', 'password': 'bad'}


class LoginThrottleTests(TestCase):
    def test_login_throttle_shared_with_admin_expires_and_does_not_lock_account(self):
        user = get_user_model().objects.create_superuser('account', 'account@example.test', 'valid-password')
        url = reverse('login')
        data = {'username': user.email, 'password': 'incorrect'}
        for i in range(10):
            target = reverse('admin:login') if i % 2 else url
            self.assertEqual(self.client.post(target, data).status_code, 200)
        rejected = self.client.post(url, data)
        self.assertEqual(rejected.status_code, 429)
        self.assertIn('Retry-After', rejected)
        self.assertEqual(self.client.get(url).status_code, 200)
        # Inny adres nadal może się zalogować: konto ofiary nie jest blokowane.
        response = self.client.post(url, {**data, 'password': 'valid-password'}, REMOTE_ADDR='192.0.2.7')
        self.assertEqual(response.status_code, 302)
        self.client.logout()
        with patch('core.auth_throttle.timezone.now', return_value=timezone.now() + timedelta(minutes=16)):
            self.assertEqual(self.client.post(url, data).status_code, 200)
        # Klucze to skróty HMAC – w bazie nie ma adresów e-mail.
        keys = AuthenticationAttempt.objects.values_list('key', flat=True)
        self.assertTrue(all(len(key) == 64 and '@' not in key for key in keys))

    def test_reset_is_limited_without_disclosing_account_existence(self):
        for _ in range(5):
            response = self.client.post(reverse('password_reset'), {'email': 'unknown@example.test'})
            self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse('password_reset'), {'email': 'unknown@example.test'})
        self.assertEqual(response.status_code, 429)

    def test_forwarded_header_does_not_bypass_limit(self):
        for i in range(10):
            self.client.post(reverse('login'), BAD_LOGIN, HTTP_X_FORWARDED_FOR=f'192.0.2.{i}')
        response = self.client.post(reverse('login'), BAD_LOGIN, HTTP_X_FORWARDED_FOR='198.51.100.1')
        self.assertEqual(response.status_code, 429)

    def test_real_ip_header_is_ignored_by_default(self):
        """Bez jawnej konfiguracji nagłówek X-Real-IP od klienta nie tworzy nowych liczników."""
        for i in range(10):
            self.client.post(reverse('login'), BAD_LOGIN, HTTP_X_REAL_IP=f'192.0.2.{i}')
        response = self.client.post(reverse('login'), BAD_LOGIN, HTTP_X_REAL_IP='198.51.100.1')
        self.assertEqual(response.status_code, 429)

    @override_settings(AUTH_THROTTLE_CLIENT_IP_HEADER='HTTP_X_REAL_IP')
    def test_pythonanywhere_uses_trusted_client_address_not_shared_proxy(self):
        for _ in range(10):
            self.client.post(reverse('login'), BAD_LOGIN, REMOTE_ADDR='10.0.0.1', HTTP_X_REAL_IP='192.0.2.1')
        response = self.client.post(reverse('login'), BAD_LOGIN, REMOTE_ADDR='10.0.0.1', HTTP_X_REAL_IP='192.0.2.2')
        self.assertEqual(response.status_code, 200)

    @override_settings(AUTH_THROTTLE_CLIENT_IP_HEADER='HTTP_X_REAL_IP')
    def test_account_ceiling_stops_guessing_but_not_the_owner(self):
        """Limit konta zatrzymuje zgadywanie z wielu adresów, a właściciel z poprawnym hasłem nadal wchodzi."""
        user = get_user_model().objects.create_user('victim', 'victim@example.test', 'valid-password')
        data = {'username': user.email, 'password': 'incorrect'}
        with patch.object(auth_throttle, 'ACCOUNT_LOGIN_LIMIT', 5):
            for i in range(5):
                response = self.client.post(reverse('login'), data, HTTP_X_REAL_IP=f'192.0.2.{i}')
                self.assertEqual(response.status_code, 200)
            response = self.client.post(reverse('login'), data, HTTP_X_REAL_IP='198.51.100.9')
            self.assertEqual(response.status_code, 429)
            # Inne konta nie są objęte limitem.
            other = {'username': 'other@example.test', 'password': 'bad'}
            self.assertEqual(self.client.post(reverse('login'), other, HTTP_X_REAL_IP='198.51.100.9').status_code, 200)
            good = {'username': user.email, 'password': 'valid-password'}
            self.assertEqual(self.client.post(reverse('login'), good, HTTP_X_REAL_IP='198.51.100.8').status_code, 302)

    def test_successful_login_clears_account_ceiling(self):
        user = get_user_model().objects.create_user('owner', 'owner@example.test', 'valid-password')
        for i in range(3):
            self.client.post(reverse('login'), {'username': user.email, 'password': 'bad'}, REMOTE_ADDR=f'192.0.2.{i}')
        self.assertTrue(AuthenticationAttempt.objects.exists())
        response = self.client.post(reverse('login'), {'username': user.email, 'password': 'valid-password'},
                                    REMOTE_ADDR='192.0.2.50')
        self.assertEqual(response.status_code, 302)
        account_key = salted_hmac('cms-auth-rate', 'login:account:' + user.email, algorithm='sha256').hexdigest()
        self.assertFalse(AuthenticationAttempt.objects.filter(key=account_key).exists())


class EmailBackendTests(TestCase):
    """Logowanie adresem e-mail: wielkość liter i odstępy nie mają znaczenia, brak danych to brak konta."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user('owner', 'owner@example.test', 'valid-password')

    def test_email_is_matched_case_insensitively_and_missing_credentials_fail(self):
        backend = EmailBackend()
        self.assertEqual(backend.authenticate(None, username='  OWNER@example.test ', password='valid-password'),
                         self.user)
        for username, password in ((None, 'valid-password'), ('owner@example.test', None), ('   ', 'x'),
                                   ('owner@example.test', 'bad')):
            with self.subTest(username=username, password=password):
                self.assertIsNone(backend.authenticate(None, username=username, password=password))


class PasswordChangeTests(TemporaryActivitySpoolMixin, TestCase):
    """Zmiana hasła korzysta z układu CMS (regresja: wcześniej strona Django bez stylów)."""

    NEW = 'New-password-927!'

    @classmethod
    def setUpTestData(cls):
        # Członek zespołu z rolą – menu CMS w base.html zależy od ról.
        cls.member = create_member('member', 'Redaktor')
        cls.member.set_password('test')
        cls.member.save()

    def setUp(self):
        super().setUp()
        self.client.force_login(self.member)
    def test_password_change_uses_cms_layout_and_preserves_validation(self):
        response = self.client.get(reverse('password_change'))
        self.assertTemplateUsed(response, 'core/base.html')
        self.assertContains(response, static('core/components.css'))
        response = self.client.post(reverse('password_change'),
                                    {'old_password': 'incorrect', 'new_password1': self.NEW, 'new_password2': self.NEW})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password('test'))

    def test_password_change_success_keeps_session_and_cms_layout(self):
        response = self.client.post(reverse('password_change'),
                                    {'old_password': 'test', 'new_password1': self.NEW, 'new_password2': self.NEW},
                                    follow=True)
        self.assertTemplateUsed(response, 'registration/password_change_done.html')
        self.assertTemplateUsed(response, 'core/base.html')
        self.assertTrue(response.wsgi_request.user.is_authenticated)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(self.NEW))


class AdminLoginTests(TestCase):
    """Panel admina loguje wyłącznie aktywnego superusera; pozostali zostają na formularzu bez sesji."""

    PASSWORD = 'valid-password'

    def test_only_active_superuser_logs_into_admin(self):
        User = get_user_model()
        cases = [
            (User.objects.create_user('plain', 'plain@example.test', self.PASSWORD), None),
            (User.objects.create_user('staff', 'staff@example.test', self.PASSWORD, is_staff=True),
             'Panel administracyjny jest dostępny tylko dla superusera.'),
            (User.objects.create_superuser('inactive', 'inactive@example.test', self.PASSWORD, is_active=False),
             None),
        ]
        for user, message in cases:
            with self.subTest(user=user.username):
                response = self.client.post(reverse('admin:login'),
                                            {'username': user.email, 'password': self.PASSWORD})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
                if message:
                    self.assertContains(response, message)
                self.assertNotIn('_auth_user_id', self.client.session)
        admin = User.objects.create_superuser('admin', 'admin@example.test', self.PASSWORD)
        response = self.client.post(reverse('admin:login'), {'username': admin.email, 'password': self.PASSWORD,
                                                             'next': reverse('admin:index')})
        self.assertRedirects(response, reverse('admin:index'), fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(admin.pk))


class PasswordResetFlowTests(TemporaryActivitySpoolMixin, TestCase):
    NEW = 'Reset-password-381!'

    def test_reset_link_from_email_sets_new_password_and_allows_login(self):
        user = get_user_model().objects.create_user('forgetful', 'forgetful@example.test', 'old-password')
        response = self.client.post(reverse('password_reset'), {'email': 'forgetful@example.test'})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['forgetful@example.test'])
        link = re.search(r'https?://[^/\s]+(/\S+)', mail.outbox[0].body)[1]
        form_page = self.client.get(link, follow=True)
        self.assertEqual(form_page.status_code, 200)
        self.assertTrue(form_page.context['validlink'])
        response = self.client.post(form_page.redirect_chain[-1][0],
                                    {'new_password1': self.NEW, 'new_password2': self.NEW})
        self.assertRedirects(response, reverse('password_reset_complete'))
        user.refresh_from_db()
        self.assertTrue(user.check_password(self.NEW))
        # Link jest jednorazowy.
        self.assertFalse(self.client.get(link, follow=True).context['validlink'])
        response = self.client.post(reverse('login'), {'username': user.email, 'password': self.NEW})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['_auth_user_id'], str(user.pk))
