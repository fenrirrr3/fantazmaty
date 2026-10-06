from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.models import AuthenticationAttempt


class AuthenticationThrottleTests(TestCase):
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
        # Another address can still authenticate: the victim's account is not locked.
        response = self.client.post(url, {**data, 'password': 'valid-password'}, REMOTE_ADDR='192.0.2.7')
        self.assertEqual(response.status_code, 302)
        self.client.logout()
        with patch('core.auth_throttle.timezone.now', return_value=timezone.now()+timedelta(minutes=16)):
            self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertTrue(all(len(key)==64 and '@' not in key for key in AuthenticationAttempt.objects.values_list('key', flat=True)))

    def test_reset_is_limited_without_disclosing_account_existence(self):
        for i in range(5):
            response = self.client.post(reverse('password_reset'), {'email': 'unknown@example.test'})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(reverse('password_reset'), {'email': 'unknown@example.test'}).status_code, 429)

    def test_forwarded_header_does_not_bypass_limit(self):
        for i in range(10):
            self.client.post(reverse('login'), {'username': 'no@example.test', 'password': 'bad'}, HTTP_X_FORWARDED_FOR=f'192.0.2.{i}')
        response = self.client.post(reverse('login'), {'username': 'no@example.test', 'password': 'bad'}, HTTP_X_FORWARDED_FOR='198.51.100.1')
        self.assertEqual(response.status_code, 429)

    def test_pythonanywhere_uses_trusted_client_address_not_shared_proxy(self):
        from django.test import override_settings
        with override_settings(AUTH_THROTTLE_CLIENT_IP_HEADER='HTTP_X_REAL_IP'):
            for i in range(10):
                self.client.post(reverse('login'), {'username': 'no@example.test', 'password': 'bad'},
                                 REMOTE_ADDR='10.0.0.1', HTTP_X_REAL_IP='192.0.2.1')
            response = self.client.post(reverse('login'), {'username': 'no@example.test', 'password': 'bad'},
                                        REMOTE_ADDR='10.0.0.1', HTTP_X_REAL_IP='192.0.2.2')
            self.assertEqual(response.status_code, 200)
