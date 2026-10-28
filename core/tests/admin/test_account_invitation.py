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
from people.models import Person

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
        for case, user, value in (('obcy token', self.member, 'zly-token'), ('bez myślnika', self.member, 'xyz'),
                                  ('pusty token', self.member, ''), ('brak konta', None, token),
                                  ('znacznik czasu spoza base36', self.member, 'ż!-' + token.split('-')[1])):
            with self.subTest(case):
                self.assertFalse(invitation_token_generator.check_token(user, value))

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
        base = {'first_name': 'Zofia', 'last_name': 'Nowa', 'email': 'zofia@example.com',
                'password1': '', 'password2': ''}
        response = self.client.post(url, {**base, 'send_invitation': 'on'})
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='zofia@example.com')
        self.assertFalse(user.has_usable_password())
        self.assertEqual(mail.outbox[-1].to, ['zofia@example.com'])
        self.assertIn('Dzień dobry, Zofia!', mail.outbox[-1].body)
        # Bez zaproszenia hasło jest nadal wymagane.
        response = self.client.post(url, {**base, 'email': 'inna@example.com'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='inna@example.com').exists())
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


class PersonScreenAccountTests(TestCase):
    """Konto zakładane z ekranu osoby: login = e-mail osoby, zaproszenie po zatwierdzeniu transakcji, stan konta i przycisk zaproszenia."""

    def setUp(self):
        self.admin = create_superuser('osoba-konto-admin')
        self.client.force_login(self.admin)

    def add_person(self, **values):
        data = {'first_name': 'Zofia', 'last_name': 'Nowa', 'email': 'Zofia.Nowa@example.com', 'dropbox_email': 'zofia-box@example.com',
                'is_active': 'on', 'create_account': 'on', '_save': 'Zapisz', **values}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('admin:people_person_add'), data, follow=True)

    def test_creating_person_with_account_sends_invitation_after_commit(self):
        self.assertEqual(self.add_person().status_code, 200)
        person = Person.objects.get(last_name='Nowa')
        user = person.user
        self.assertEqual((user.username, user.email, user.first_name, user.last_name),
                         ('zofia.nowa@example.com', 'zofia.nowa@example.com', 'Zofia', 'Nowa'))
        self.assertTrue(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(person.dropbox_email, 'zofia-box@example.com')
        self.assertEqual([m.to for m in mail.outbox], [['zofia.nowa@example.com']])
        self.assertTrue(LogEntry.objects.filter(object_id=str(user.pk), change_message='Wysłano zaproszenie do CMS-u.').exists())
        page = self.client.get(reverse('admin:people_person_change', args=[person.pk]))
        for text in ('Logowanie aktywne.', 'Zaproszenie nieprzyjęte – hasło nieustawione.', 'Ostatnie zaproszenie:',
                     reverse('admin:auth_user_invite', args=[user.pk]), 'zofia.nowa@example.com – <a',
                     'otwórz konto'):
            with self.subTest(text=text):
                self.assertContains(page, text)
        # Osoba z kontem: bez tworzenia i przepinania konta.
        for absent in ('name="create_account"', 'name="user"'):
            with self.subTest(absent=absent):
                self.assertNotContains(page, absent)

    def test_account_created_concurrently_is_a_field_error(self):
        """Wyścig: konto z tym e-mailem powstaje między walidacją a zapisem (IntegrityError) – formularz wraca z błędem
        pola e-mail zamiast 500, a zapis jest wycofany: bez osoby, bez konta i bez zaproszenia."""
        from django.db import IntegrityError
        from people.admin import ACCOUNT_CONFLICT

        with patch.object(User.objects, 'create_user', side_effect=IntegrityError('UNIQUE constraint failed')) as create:
            response = self.add_person()
        self.assertEqual((response.status_code, create.call_count), (200, 1))
        self.assertEqual(response.context['adminform'].form.errors['email'], [ACCOUNT_CONFLICT])
        self.assertFalse(Person.objects.filter(last_name='Nowa').exists())
        self.assertFalse(User.objects.filter(email='zofia.nowa@example.com').exists())
        self.assertEqual(mail.outbox, [])

    def test_add_screen_links_existing_account_without_account_shortcuts(self):
        """Dodawanie osoby: pole „Powiąż istniejące konto” bez przycisków dodawania/edycji konta (konto tworzy pole
        „Utwórz konto…”) i bez pustego wiersza loginu."""
        page = self.client.get(reverse('admin:people_person_add'))
        self.assertContains(page, 'Powiąż istniejące konto')
        self.assertContains(page, 'name="create_account"')
        for absent in ('id="add_id_user"', 'id="change_id_user"', 'id="delete_id_user"', 'id="view_id_user"',
                       'field-account_link'):
            with self.subTest(absent=absent):
                self.assertNotContains(page, absent)

    def test_invitation_button_on_person_screen_returns_to_person(self):
        self.add_person()
        person = Person.objects.get(last_name='Nowa')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('admin:auth_user_invite', args=[person.user_id]), {'_from_person': '1'})
        self.assertRedirects(response, reverse('admin:people_person_change', args=[person.pk]))
        self.assertEqual(len(mail.outbox), 2)

    def test_rolled_back_person_save_creates_no_account_and_sends_no_mail(self):
        """Wycofany zapis osoby nie zostawia konta i nie wysyła zaproszenia."""
        from people.admin import PersonAdminForm
        request = RequestFactory().post('/')
        request.user, request.session = self.admin, {}
        request._messages = FallbackStorage(request)
        form = PersonAdminForm({'first_name': 'Jan', 'last_name': 'Wycofany', 'email': 'wycofany@example.com',
                                'is_active': 'on', 'create_account': 'on'})
        self.assertTrue(form.is_valid(), form.errors)
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError), transaction.atomic():
                admin.site._registry[Person].save_model(request, form.save(commit=False), form, False)
                self.assertTrue(User.objects.filter(username='wycofany@example.com').exists())
                raise RuntimeError('błąd zapisu')
        self.assertFalse(User.objects.filter(email='wycofany@example.com').exists())
        self.assertEqual(mail.outbox, [])


class DiscordInvitationTests(TestCase):
    """„Zaproś też na Discorda”: link zapraszający z ustawień w panelu trafia do maila z zaproszeniem do CMS-u."""
    INVITE = 'https://discord.gg/fantazmaty'

    @classmethod
    def setUpTestData(cls):
        from core.notification_models import DiscordSettings
        cls.admin = create_superuser('discord-zaproszenie-admin')
        cls.member = create_member('zaproszony', 'Redaktor')
        User.objects.filter(pk=cls.member.pk).update(email='zaproszony@example.com')
        cls.settings_model = DiscordSettings

    def setUp(self):
        self.client.force_login(self.admin)

    def set_link(self, url):
        self.settings_model.objects.update_or_create(pk=1, defaults={'invite_url': url})

    def invite(self, **data):
        """Przycisk zaproszenia; zwraca komunikaty panelu (dodawane po zatwierdzeniu transakcji)."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('admin:auth_user_invite', args=[self.member.pk]), data)
        return [str(message) for message in response.wsgi_request._messages]

    def test_link_validation(self):
        """Link: tylko https://discord.gg/… albo https://discord.com/invite/…; pusty – dozwolony (opcja wyłączona)."""
        from django.core.exceptions import ValidationError
        cases = {'discord.gg': ('https://discord.gg/abc-123', True), 'discord.com': ('https://discord.com/invite/abc', True),
                 'www': ('https://www.discord.com/invite/abc/', True), 'pusty': ('', True),
                 'http': ('http://discord.gg/abc', False), 'obca domena': ('https://discord.gg.evil.example/abc', False),
                 'kanał zamiast zaproszenia': ('https://discord.com/channels/1/2', False),
                 'bez kodu': ('https://discord.gg/', False)}
        for case, (url, valid) in cases.items():
            with self.subTest(case):
                record = self.settings_model(invite_url=url)
                if valid:
                    record.full_clean()
                else:
                    with self.assertRaisesMessage(ValidationError, 'link zaproszenia Discorda'):
                        record.full_clean()

    def test_settings_page_saves_link(self):
        """Ustawienia w panelu: jeden rekord, zapis poprawnego linku, błąd przy złym."""
        url = reverse('admin:core_discordsettings_add')
        response = self.client.post(url, {'invite_url': 'https://example.com/x'})
        self.assertContains(response, 'Podaj link zaproszenia Discorda')
        self.client.post(url, {'invite_url': self.INVITE})
        self.assertEqual(self.settings_model.invite_url_value(), self.INVITE)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_new_account_form_option_depends_on_link(self):
        """Formularz nowego konta: bez linku pole nieaktywne z podpowiedzią i mail bez Discorda; z linkiem –
        domyślnie zaznaczone, a mail zawiera link (odznaczone – bez linku)."""
        url = reverse('admin:auth_user_add')
        page = self.client.get(url)
        self.assertContains(page, 'Link zapraszający na Discorda nie jest ustawiony')
        self.assertRegex(page.content.decode(), r'name="invite_discord"[^>]* disabled')
        base = {'first_name': 'Zofia', 'last_name': 'Nowa', 'password1': '', 'password2': '', 'send_invitation': 'on'}
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url, {**base, 'email': 'bez-linku@example.com', 'invite_discord': 'on'})
        self.assertNotIn('Discord', mail.outbox[-1].body)
        self.set_link(self.INVITE)
        self.assertContains(self.client.get(url), 'id="id_invite_discord" checked')
        for email, data, expected in (('z-linkiem@example.com', {'invite_discord': 'on'}, True),
                                      ('odznaczone@example.com', {}, False)):
            with self.subTest(email), self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, {**base, 'email': email, **data})
            self.assertEqual(mail.outbox[-1].to, [email])
            self.assertEqual(self.INVITE in mail.outbox[-1].body, expected)

    def test_invite_button_with_and_without_discord(self):
        """Przycisk zaproszenia: pole „Zaproś też na Discorda” przy przycisku (zaznaczone) – link w mailu
        i w komunikacie; bez zaznaczenia albo bez linku w ustawieniach – mail bez Discorda."""
        self.assertNotIn('name="_invite_discord"',
                         self.client.get(reverse('admin:auth_user_change', args=[self.member.pk])).content.decode())
        self.invite(_invite_discord='1')
        self.assertNotIn('discord', mail.outbox[-1].body.lower())
        self.set_link(self.INVITE)
        for page in (reverse('admin:auth_user_change', args=[self.member.pk]),
                     reverse('admin:people_person_change', args=[self.member.person_profile.pk])):
            with self.subTest(page):
                self.assertContains(self.client.get(page), 'name="_invite_discord" value="1" checked')
        self.assertEqual(self.invite(_invite_discord='1'),
                         ['Wysłano zaproszenie na adres zaproszony@example.com (z zaproszeniem na Discorda).'])
        body = mail.outbox[-1].body
        self.assertIn(f'Zapraszamy też na serwer Fantazmatów na Discordzie:\n{self.INVITE}\n', body)
        self.assertIn('Po dołączeniu od razu ustaw swój pseudonim na imię + nazwisko, jeśli twoje konto nazywa się '
                      'inaczej.\n\nZespół Fantazmatów', body)
        self.invite()
        self.assertNotIn(self.INVITE, mail.outbox[-1].body)
        self.assertIn('ponowne zaproszenie.\n\nZespół Fantazmatów', mail.outbox[-1].body)

    def test_person_screen_account_with_discord(self):
        """Nowa osoba z „Utwórz konto” i „Zaproś też na Discorda” – mail z linkiem."""
        self.set_link(self.INVITE)
        page = self.client.get(reverse('admin:people_person_add'))
        self.assertContains(page, 'name="invite_discord"')
        data = {'first_name': 'Ewa', 'last_name': 'Nowa', 'email': 'ewa.nowa@example.com', 'is_active': 'on',
                'create_account': 'on', 'invite_discord': 'on', '_save': 'Zapisz'}
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('admin:people_person_add'), data)
        self.assertEqual(mail.outbox[-1].to, ['ewa.nowa@example.com'])
        self.assertIn(self.INVITE, mail.outbox[-1].body)
