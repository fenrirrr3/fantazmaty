"""Zaproszenie nowej osoby: przycisk w panelu konta i opcja przy zakładaniu konta wysyłają link do ustawienia hasła."""
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.db import transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse

from core.invitations import invitation_token_generator
from fantazmaty.testing import create_member, create_superuser

User = get_user_model()
LINK = re.compile(r'http://testserver(/konto/zaproszenie/[^/\s]+/[^/\s]+/)')


class AccountInvitationTests(TransactionTestCase):
    """Mail wychodzi po zatwierdzeniu transakcji, więc testy przez klienta HTTP wymagają prawdziwych commitów
    (w TestCase on_commit nie wykonałby się w trakcie żądania i komunikaty admina nie trafiłyby na stronę)."""

    def setUp(self):
        self.admin = create_superuser('zaproszenia-admin')
        self.member = create_member('nowa-osoba', 'Redaktor')
        self.member.email = 'nowa@example.com'
        self.member.set_unusable_password()
        self.member.save()
        self.client.force_login(self.admin)

    def invite(self, user=None):
        return self.client.post(reverse('admin:auth_user_invite', args=[(user or self.member).pk]), follow=True)

    def link(self):
        return LINK.search(mail.outbox[-1].body).group(1)

    def test_button_sends_invitation_and_is_logged(self):
        page = self.client.get(reverse('admin:auth_user_change', args=[self.member.pk]))
        self.assertContains(page, 'Wyślij zaproszenie')
        self.assertContains(page, 'Nie wysłano jeszcze zaproszenia.')
        response = self.invite()
        self.assertContains(response, 'Wysłano zaproszenie na adres nowa@example.com.')
        self.assertContains(response, 'Ostatnie zaproszenie:')
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual((message.subject, message.to), ('Zaproszenie do Fantazmaty CMS', ['nowa@example.com']))
        self.assertIn('ważny przez 7 dni', message.body)
        self.assertIn('http://testserver/konto/logowanie/', message.body)
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.member.pk),
                                                change_message='Wysłano zaproszenie do CMS-u.').exists())

    def test_link_sets_password_once(self):
        self.invite()
        self.client.logout()
        form_url = self.client.get(self.link(), follow=True).redirect_chain[-1][0]
        page = self.client.get(form_url)
        self.assertContains(page, 'Witaj w Fantazmaty CMS')
        response = self.client.post(form_url, {'new_password1': 'Bardzo-trudne-haslo-42', 'new_password2': 'Bardzo-trudne-haslo-42'})
        self.assertRedirects(response, reverse('password_reset_complete'))
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password('Bardzo-trudne-haslo-42'))
        self.assertContains(self.client.get(self.link(), follow=True), 'Link z zaproszenia wygasł albo został już użyty.')

    def test_link_is_valid_for_a_week_not_only_an_hour(self):
        token = invitation_token_generator.make_token(self.member)
        now = invitation_token_generator._now()
        cases = {'after 3 days': (timedelta(days=3), True), 'after 8 days': (timedelta(days=8), False)}
        for case, (later, valid) in cases.items():
            with self.subTest(case), patch.object(invitation_token_generator, '_now', return_value=now + later):
                self.assertEqual(invitation_token_generator.check_token(self.member, token), valid)
        self.assertFalse(invitation_token_generator.check_token(self.member, 'zly-token'))
        self.assertFalse(invitation_token_generator.check_token(self.member, 'xyz'))

    def test_inactive_account_or_missing_email_get_no_mail(self):
        cases = {'inactive': ({'is_active': False}, 'Konto jest nieaktywne'),
                 'no email': ({'email': ''}, 'Konto nie ma adresu e-mail')}
        for number, (case, (changes, error)) in enumerate(cases.items()):
            user = create_member(f'bez-zaproszenia-{number}', 'Redaktor')
            User.objects.filter(pk=user.pk).update(**changes)
            with self.subTest(case):
                self.assertContains(self.invite(user), 'Nie wysłano zaproszenia: ' + error)
        self.assertEqual(mail.outbox, [])

    def test_mail_server_error_is_reported(self):
        with patch('core.invitations.send_mail', side_effect=OSError('smtp')):
            response = self.invite()
        self.assertContains(response, 'Nie udało się wysłać wiadomości')
        self.assertFalse(LogEntry.objects.filter(change_message='Wysłano zaproszenie do CMS-u.').exists())

    def test_invitation_needs_post_and_superuser(self):
        url = reverse('admin:auth_user_invite', args=[self.member.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.force_login(create_member('koordynator-konta', 'Koordynator'))
        self.assertIn(self.client.post(url).status_code, (302, 403))
        self.assertEqual(mail.outbox, [])

    def test_new_account_with_invitation_needs_no_password(self):
        url = reverse('admin:auth_user_add')
        base = {'username': 'zaproszona', 'first_name': 'Zofia', 'last_name': 'Nowa', 'email': 'zofia@example.com',
                'password1': '', 'password2': ''}
        response = self.client.post(url, {**base, 'send_invitation': 'on'})
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='zaproszona')
        self.assertFalse(user.has_usable_password())
        self.assertEqual(mail.outbox[-1].to, ['zofia@example.com'])
        self.assertIn('Dzień dobry, Zofia!', mail.outbox[-1].body)
        # Bez zaproszenia hasło jest nadal wymagane.
        response = self.client.post(url, {**base, 'username': 'bez-hasla', 'email': 'inna@example.com'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='bez-hasla').exists())
        self.assertEqual(len(mail.outbox), 1)


class InvitationRollbackTests(TestCase):
    def test_rolled_back_transaction_sends_no_mail(self):
        """Zaproszenie zlecone w transakcji, która została wycofana (np. błąd zapisu konta), nie wychodzi;
        po zatwierdzeniu mail wychodzi, a zaproszenie jest odnotowane w dzienniku."""
        admin_user = create_superuser('zaproszenia-rollback')
        member = create_member('wycofana-osoba', 'Redaktor')
        User.objects.filter(pk=member.pk).update(email='wycofana@example.com')
        member.refresh_from_db()
        request = RequestFactory().post('/')
        request.user, request.session = admin_user, {}
        request._messages = FallbackStorage(request)
        model_admin = admin.site._registry[User]
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError), transaction.atomic():
                model_admin._invite(request, member)
                raise RuntimeError('błąd zapisu')
        self.assertEqual(mail.outbox, [])
        self.assertFalse(LogEntry.objects.filter(object_id=str(member.pk)).exists())
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                model_admin._invite(request, member)
            self.assertEqual(mail.outbox, [])
        self.assertEqual([m.to for m in mail.outbox], [['wycofana@example.com']])
        self.assertTrue(LogEntry.objects.filter(object_id=str(member.pk), change_message='Wysłano zaproszenie do CMS-u.').exists())
        self.assertEqual([str(m) for m in request._messages], ['Wysłano zaproszenie na adres wycofana@example.com.'])
