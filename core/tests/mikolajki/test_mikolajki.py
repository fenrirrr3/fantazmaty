"""Mikołajki: moduł włączany w panelu, zapisy bez konta z potwierdzeniem e-mailem i kodem uczestnika, własne
zgłoszenie po kodzie, zamknięcie zapisów, losowanie jednego koła bez par wzajemnych, listy par, paczka
przygotowana i wysyłka adresu pary z panelu admina."""
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.core import mail
from django.test import TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from core.models import OptionalModules
from fantazmaty.testing import create_superuser, edit_token
from mikolajki import services
from mikolajki.models import LETTER_LIMIT, Edition, Entry

SIGNUP = reverse('mikolajki:secret_santa')
ENTRY = reverse('mikolajki:secret_santa_entry')
CODE_RE = re.compile(r'\b[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}\b')


def form_data(**changes):
    data = {'email': 'Anna@Example.test', 'full_name': '  Anna   Nowak ', 'shipping_address': 'Paczkomat KRA01M',
            'phone': '+48 600 100 200', 'letter': 'Lubię fantastykę i herbatę.', 'consent': 'on', 'website': ''}
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


class MikolajkiMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        OptionalModules.objects.update_or_create(pk=1, defaults={'secret_santa': True})
        cls.edition = Edition.objects.get(year=2027)
        Edition.objects.filter(pk=cls.edition.pk).update(signup_deadline=timezone.now() + timedelta(days=7))
        cls.edition.refresh_from_db()

    def entry(self, name, *, confirmed=True, **values):
        return Entry.objects.create(
            edition=self.edition, email=f'{name.lower()}@example.test', full_name=name, shipping_address=f'ul. {name} 1',
            phone='600100200', letter=f'Lista życzeń: {name[::-1]}.', code=services.new_code(),
            confirmed_at=timezone.now() if confirmed else None, **values)

    def close(self):
        Edition.objects.filter(pk=self.edition.pk).update(closed_at=timezone.now())
        self.edition.refresh_from_db()


class ModuleSwitchTests(MikolajkiMixin, TestCase):
    def test_disabled_module_hides_page_and_admin_section(self):
        """Wyłączony moduł: strony zwracają 404, a sekcja „Mikołajki” znika z panelu (adresy panelu – 403);
        przełącznik jest w Ustawieniach i otwiera od razu edycję jedynego rekordu."""
        superuser = create_superuser('mikolajki-admin')
        self.client.force_login(superuser)
        token = services.confirm_token(self.entry('Ala'))
        pages = (SIGNUP, ENTRY, reverse('mikolajki:secret_santa_confirm', args=[token]))
        for enabled in (True, False):
            OptionalModules.objects.filter(pk=1).update(secret_santa=enabled)
            with self.subTest(enabled=enabled):
                for url in pages:
                    self.assertEqual(self.client.get(url).status_code == 404, not enabled, url)
                groups = [group['app_label'] for group in self.client.get(reverse('admin:index')).context['app_list']]
                self.assertEqual('mikolajki' in groups, enabled)
                self.assertEqual(self.client.get(reverse('admin:mikolajki_entry_changelist')).status_code,
                                 200 if enabled else 403)
        self.assertRedirects(self.client.get(reverse('admin:core_optionalmodules_changelist')),
                             reverse('admin:core_optionalmodules_change', args=[1]))
        OptionalModules.objects.filter(pk=1).update(secret_santa=False)
        self.client.post(reverse('admin:core_optionalmodules_change', args=[1]),
                         {'secret_santa': 'on', '_edit_version': edit_token(superuser, OptionalModules.objects.get())})
        self.assertTrue(OptionalModules.enabled('secret_santa'))
        self.client.logout()
        self.assertEqual(self.client.get(SIGNUP).status_code, 200)

    def test_first_edition_is_2027_with_fantazmaty_branding(self):
        page = self.client.get(SIGNUP)
        self.assertContains(page, 'Mikołajki 2027')
        self.assertContains(page, 'fantazmaty-logo')
        self.assertEqual((page['X-Robots-Tag'], page['Referrer-Policy']), ('noindex, nofollow', 'same-origin'))
        self.assertContains(page, 'name="website"')


class SignupTests(MikolajkiMixin, TestCase):
    def signup(self, **changes):
        return self.client.post(SIGNUP, form_data(**changes))

    def test_signup_sends_summary_code_and_confirmation_link(self):
        """Zgłoszenie: dane znormalizowane, kod XXXX-XXXX-XXXX-XXXX, wiadomość z podsumowaniem, kodem i linkiem;
        dopiero kliknięcie linku potwierdza udział (ponowne – bez zmian)."""
        response = self.signup()
        self.assertRedirects(response, SIGNUP, fetch_redirect_response=False)
        entry = Entry.objects.get()
        self.assertEqual((entry.email, entry.full_name, entry.confirmed_at, entry.source),
                         ('anna@example.test', 'Anna Nowak', None, Entry.Source.FORM))
        self.assertRegex(entry.code, CODE_RE)
        self.assertIsNotNone(entry.consent_at)
        message = mail.outbox[-1]
        self.assertEqual(message.to, ['anna@example.test'])
        self.assertIn('potwierdź zgłoszenie', message.subject)
        for text in ('Anna Nowak', 'Paczkomat KRA01M', '+48 600 100 200', 'Lubię fantastykę i herbatę.', entry.code):
            self.assertIn(text, message.body)
        link = re.search(r'http://testserver(/mikolajki/potwierdz/\S+/)', message.body)[1]
        self.assertContains(self.client.get(SIGNUP), 'Wysłaliśmy wiadomość na adres anna@example.test')
        self.assertContains(self.client.get(link), 'Zgłoszenie jest potwierdzone')
        entry.refresh_from_db()
        confirmed = entry.confirmed_at
        self.assertIsNotNone(confirmed)
        self.assertContains(self.client.get(link), 'było już potwierdzone')
        entry.refresh_from_db()
        self.assertEqual(entry.confirmed_at, confirmed)
        self.assertEqual(self.client.get(reverse('mikolajki:secret_santa_confirm', args=['zly'])).status_code, 404)

    def test_form_passes_csrf_check_with_same_origin_header(self):
        """Regresja: przy „Referrer-Policy: no-referrer” przeglądarka wysyła „Origin: null” i CSRF odrzucał
        zgłoszenie; strona ma politykę same-origin, a formularz z nagłówkiem Origin strony przechodzi."""
        from django.test import Client
        from fantazmaty.testing import form_value
        client = Client(enforce_csrf_checks=True)
        page = client.get(SIGNUP)
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        self.assertContains(page, '<meta name="referrer" content="same-origin">')
        data = {**form_data(), 'csrfmiddlewaretoken': form_value(page, 'csrfmiddlewaretoken')}
        self.assertEqual(client.post(SIGNUP, data, HTTP_ORIGIN='null').status_code, 403)
        self.assertRedirects(client.post(SIGNUP, data, HTTP_ORIGIN='http://testserver'), SIGNUP,
                             fetch_redirect_response=False)

    def test_invalid_data_and_bots_save_nothing(self):
        """Brak zgody, zły telefon i za długi list – błędy formularza; wypełnione pole-pułapka – odpowiedź jak przy
        zgłoszeniu, ale bez zapisu i wiadomości."""
        for name, changes, error in (('bez zgody', {'consent': None}, 'Zaznacz zgodę'),
                                     ('telefon', {'phone': '12ab'}, 'co najmniej 9 cyfr'),
                                     ('list', {'letter': 'x' * (LETTER_LIMIT + 1)}, str(LETTER_LIMIT)),
                                     ('e-mail', {'email': 'nie-adres'}, 'adres e-mail')):
            with self.subTest(name):
                response = self.signup(**changes)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, error, status_code=400)
        response = self.signup(website='http://spam.test')
        self.assertRedirects(response, SIGNUP)
        self.assertFalse(Entry.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_repeated_signups(self):
        """Niepotwierdzone i wycofane zgłoszenie dostaje nowe dane i nowy link (ten sam kod); potwierdzone
        zostaje bez zmian, a osoba dostaje przypomnienie kodu."""
        self.signup()
        entry = Entry.objects.get()
        self.signup(letter='Nowy list.')
        entry.refresh_from_db()
        self.assertEqual((entry.letter, Entry.objects.count()), ('Nowy list.', 1))
        self.assertIn('potwierdź', mail.outbox[-1].subject)
        Entry.objects.filter(pk=entry.pk).update(confirmed_at=timezone.now())
        self.signup(letter='Próba nadpisania.')
        entry.refresh_from_db()
        self.assertEqual(entry.letter, 'Nowy list.')
        self.assertIn('masz już zgłoszenie', mail.outbox[-1].subject)
        self.assertIn(entry.code, mail.outbox[-1].body)
        Entry.objects.filter(pk=entry.pk).update(withdrawn_at=timezone.now())
        self.signup(letter='Wracam.')
        entry.refresh_from_db()
        self.assertEqual((entry.letter, entry.withdrawn_at, entry.confirmed_at), ('Wracam.', None, None))

    def test_closed_signups(self):
        """Po terminie albo „Zamknij teraz”: bez formularza, zgłoszenie odrzucone, link nie potwierdza."""
        self.signup()
        link = re.search(r'http://testserver(/mikolajki/potwierdz/\S+/)', mail.outbox[-1].body)[1]
        for name, close in (('termin', lambda: Edition.objects.filter(pk=self.edition.pk).update(
                signup_deadline=timezone.now() - timedelta(minutes=1))), ('zamknij teraz', self.close)):
            with self.subTest(name):
                close()
                page = self.client.get(SIGNUP)
                self.assertNotContains(page, 'name="consent"')
                self.assertContains(page, 'Zapisy są zamknięte')
                self.assertRedirects(self.signup(email='inna@example.test'), SIGNUP)
                self.assertFalse(Entry.objects.filter(email='inna@example.test').exists())
                self.assertContains(self.client.get(link), 'Zapisy są już zamknięte')
                self.assertIsNone(Entry.objects.get().confirmed_at)
                Edition.objects.filter(pk=self.edition.pk).update(
                    signup_deadline=timezone.now() + timedelta(days=1), closed_at=None)

    @override_settings(AUTH_THROTTLE_ENABLED=True)
    def test_attempts_from_one_address_are_limited(self):
        for _ in range(services.LIMITS['signup'][0]):
            self.signup(email='x@example.test')
        self.assertEqual(self.signup(email='y@example.test').status_code, 429)
        for _ in range(services.LIMITS['code'][0]):
            self.client.post(ENTRY, {'action': 'login', 'email': 'x@example.test', 'code': 'ZLY'})
        response = self.client.post(ENTRY, {'action': 'login', 'email': 'x@example.test', 'code': 'ZLY'})
        self.assertEqual(response.status_code, 429)

    def test_mail_failure_shows_message_and_keeps_form(self):
        with patch('mikolajki.services.send_mail', side_effect=OSError('smtp')):
            response = self.signup()
        self.assertContains(response, 'Nie udało się wysłać wiadomości e-mail', status_code=400)
        self.assertEqual(Entry.objects.count(), 1)
        self.assertRedirects(self.signup(), SIGNUP)  # ponowne wysłanie formularza wysyła link
        self.assertEqual(len(mail.outbox), 1)


class OwnEntryTests(MikolajkiMixin, TestCase):
    def login(self, entry, code=None):
        return self.client.post(ENTRY, {'action': 'login', 'email': entry.email.upper(),
                                        'code': code or entry.code.replace('-', '').lower()})

    def test_login_edit_resend_and_withdraw(self):
        """Wejście adresem i kodem (wielkość liter i kreski bez znaczenia), zmiana danych z podsumowaniem
        e-mailem, ponowny link dla niepotwierdzonego i wycofanie zgłoszenia."""
        entry = self.entry('Ala', confirmed=False)
        other = self.entry('Ola')
        self.assertContains(self.login(entry, other.code), 'Nie znaleźliśmy zgłoszenia', status_code=400)
        self.assertRedirects(self.login(entry), ENTRY)
        page = self.client.get(ENTRY)
        self.assertContains(page, 'Niepotwierdzone')
        self.assertNotContains(page, other.letter)
        self.client.post(ENTRY, {'action': 'resend'})
        self.assertIn('potwierdź', mail.outbox[-1].subject)
        response = self.client.post(ENTRY, {'action': 'save', 'full_name': 'Ala Kot', 'shipping_address': 'ul. Nowa 2',
                                            'phone': '600 100 200', 'letter': 'Zmieniony list.'})
        self.assertRedirects(response, ENTRY)
        entry.refresh_from_db()
        self.assertEqual((entry.full_name, entry.letter), ('Ala Kot', 'Zmieniony list.'))
        self.assertIn('zmienione zgłoszenie', mail.outbox[-1].subject)
        self.assertContains(self.client.post(ENTRY, {'action': 'save', 'full_name': '', 'shipping_address': 'x',
                                                     'phone': '1', 'letter': 'x'}), 'errorlist', status_code=400)
        self.assertRedirects(self.client.post(ENTRY, {'action': 'withdraw'}), SIGNUP)
        entry.refresh_from_db()
        self.assertIsNotNone(entry.withdrawn_at)
        self.assertContains(self.client.get(ENTRY), 'name="code"')  # wylogowane
        self.assertContains(self.login(entry), 'Nie znaleźliśmy zgłoszenia', status_code=400)

    def test_after_closing_data_is_read_only(self):
        entry = self.entry('Ala')
        self.close()
        self.login(entry)
        page = self.client.get(ENTRY)
        self.assertNotContains(page, 'value="save"')
        self.assertContains(page, 'losowanie wkrótce')
        self.client.post(ENTRY, {'action': 'save', 'full_name': 'Zmiana', 'shipping_address': 'x', 'phone': '600100200',
                                 'letter': 'x'})
        entry.refresh_from_db()
        self.assertEqual(entry.full_name, 'Ala')


class DrawTests(MikolajkiMixin, TestCase):
    def test_draw_makes_one_circle_without_mutual_pairs_and_sends_letters(self):
        """Losowanie: tylko po zamknięciu, co najmniej trzy potwierdzone zgłoszenia, raz; jedno koło przez
        wszystkich potwierdzonych (bez siebie i bez par wzajemnych), niepotwierdzone i wycofane poza losowaniem;
        każdy dostaje e-mailem tylko list swojej pary."""
        people = [self.entry(name) for name in ('Ala', 'Bartek', 'Celina')]
        with self.assertRaisesMessage(services.DrawError, 'Najpierw zamknij zapisy'):
            services.draw(self.edition)
        self.close()
        Entry.objects.filter(pk=people[2].pk).update(withdrawn_at=timezone.now())
        with self.assertRaisesMessage(services.DrawError, 'co najmniej trzy'):
            services.draw(self.edition)
        Entry.objects.filter(pk=people[2].pk).update(withdrawn_at=None)
        people += [self.entry(name) for name in ('Darek', 'Ewa')]
        pending, withdrawn = self.entry('Franek', confirmed=False), self.entry('Gosia', withdrawn_at=timezone.now())
        circle = services.draw(self.edition)
        self.assertEqual(len(circle), 5)
        entries = {entry.pk: entry for entry in Entry.objects.filter(edition=self.edition)}
        self.assertIsNone(entries[pending.pk].recipient_id)
        self.assertIsNone(entries[withdrawn.pk].recipient_id)
        start = people[0].pk
        visited, current = [], start
        while True:
            visited.append(current)
            current = entries[current].recipient_id
            if current == start:
                break
        self.assertEqual(sorted(visited), sorted(entry.pk for entry in people))
        for entry in people:
            recipient = entries[entries[entry.pk].recipient_id]
            self.assertNotEqual(recipient.pk, entry.pk)
            self.assertNotEqual(recipient.recipient_id, entry.pk)
        with self.assertRaisesMessage(services.DrawError, 'już rozlosowane'):
            services.draw(self.edition)
        request = self.client.get(SIGNUP).wsgi_request
        mail.outbox.clear()
        self.assertEqual(services.send_letters(self.edition, request), (5, []))
        self.assertEqual(services.send_letters(self.edition, request), (0, []))
        for message in mail.outbox:
            giver = Entry.objects.get(email=message.to[0])
            recipient = giver.recipient
            self.assertIn(recipient.letter, message.body)
            self.assertNotIn(recipient.full_name, message.body)
            self.assertNotIn(recipient.shipping_address, message.body)
            self.assertIn(giver.code, message.body)

    @tag('slow')
    def test_every_circle_order_appears(self):
        """Losowość: dla czterech osób jest 6 różnych kół – w 100 losowaniach pojawia się każde."""
        people = [self.entry(name) for name in ('Ala', 'Bartek', 'Celina', 'Darek')]
        self.close()
        seen = set()
        for _ in range(100):
            Edition.objects.filter(pk=self.edition.pk).update(drawn_at=None)
            Entry.objects.filter(edition=self.edition).update(recipient=None)
            services.draw(self.edition)
            recipients = dict(Entry.objects.filter(edition=self.edition).values_list('pk', 'recipient_id'))
            seen.add(tuple(recipients[entry.pk] for entry in people))
        self.assertEqual(len(seen), 6)

    def test_draw_uses_cryptographic_shuffle(self):
        for name in ('Ala', 'Bartek', 'Celina'):
            self.entry(name)
        self.close()
        with patch('mikolajki.services.secrets.SystemRandom') as random:
            services.draw(self.edition)
        random.return_value.shuffle.assert_called_once()


class PackageAndAdminTests(MikolajkiMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('mikolajki-panel')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_admin_close_draw_letters_package_and_address(self):
        """Panel: „Zamknij zapisy teraz”, „Rozlosuj pary i wyślij listy” (błąd poczty – „Wyślij niewysłane listy”),
        na stronie „Paczka przygotowana” i list pary, w panelu stan zgłoszenia i wysłanie adresu pary."""
        people = [self.entry(name) for name in ('Ala', 'Bartek', 'Celina')]
        change = reverse('admin:mikolajki_edition_change', args=[self.edition.pk])
        page = self.client.get(change)
        self.assertContains(page, 'Zamknij zapisy teraz')
        self.assertNotContains(page, 'Rozlosuj pary')
        self.client.post(reverse('admin:mikolajki_edition_close', args=[self.edition.pk]))
        self.edition.refresh_from_db()
        self.assertIsNotNone(self.edition.closed_at)
        self.assertContains(self.client.get(change), 'Rozlosuj pary i wyślij listy')
        failing = people[0].email
        real_send = services.send_mail

        def send(subject, body, sender, to):
            if to == [failing]:
                raise OSError('smtp')
            return real_send(subject, body, sender, to)

        with patch('mikolajki.services.send_mail', side_effect=send):
            response = self.client.post(reverse('admin:mikolajki_edition_draw', args=[self.edition.pk]), follow=True)
        self.assertContains(response, 'Rozlosowano pary: 3')
        self.assertContains(response, f'Nie udało się wysłać listów do: {failing}')
        self.assertContains(response, 'Wyślij niewysłane listy (1)')
        self.client.post(reverse('admin:mikolajki_edition_letters', args=[self.edition.pk]))
        self.assertFalse(Entry.objects.filter(letter_sent_at__isnull=True).exists())
        self.assertEqual(self.client.get(reverse('admin:mikolajki_edition_draw', args=[self.edition.pk])).status_code,
                         405)
        # Uczestnik: list pary i „Paczka przygotowana”.
        giver = Entry.objects.select_related('recipient').get(pk=people[0].pk)
        self.client.logout()
        self.client.post(ENTRY, {'action': 'login', 'email': giver.email, 'code': giver.code})
        page = self.client.get(ENTRY)
        self.assertContains(page, giver.recipient.letter)
        self.assertNotContains(page, giver.recipient.shipping_address)
        self.client.post(ENTRY, {'action': 'prepared'})
        giver.refresh_from_db()
        self.assertIsNotNone(giver.package_prepared_at)
        # Panel: stan i wysłanie adresu pary (tylko przy przygotowanej paczce).
        self.client.force_login(self.admin)
        changelist = self.client.get(reverse('admin:mikolajki_entry_changelist') + '?stan=prepared')
        self.assertEqual([entry.pk for entry in changelist.context['cl'].result_list], [giver.pk])
        mail.outbox.clear()
        other = Entry.objects.exclude(pk=giver.pk).first()
        response = self.client.post(reverse('admin:mikolajki_entry_changelist'),
                                    {'action': 'send_addresses', '_selected_action': [giver.pk, other.pk]}, follow=True)
        self.assertContains(response, 'Wysłano adresy par: 1')
        self.assertContains(response, 'paczka nie jest oznaczona jako przygotowana')
        self.assertEqual(mail.outbox[0].to, [giver.email])
        for text in (giver.recipient.full_name, giver.recipient.shipping_address, giver.recipient.phone):
            self.assertIn(text, mail.outbox[0].body)
        giver.refresh_from_db()
        self.assertIsNotNone(giver.address_sent_at)
        page = self.client.get(reverse('admin:mikolajki_entry_change', args=[giver.pk]))
        self.assertContains(page, 'Wyślij adres pary ponownie')
        self.assertContains(page, giver.code)
        self.client.post(reverse('admin:mikolajki_entry_address', args=[giver.pk]))
        self.assertEqual(len(mail.outbox), 2)

    def test_manual_entry_and_lookup_by_code(self):
        """Ręczne dodanie: od razu potwierdzone, z kodem i podsumowaniem e-mailem; ten sam adres w edycji – błąd;
        po losowaniu dodanie niemożliwe. Wyszukiwanie po kodzie identyfikuje uczestnika."""
        add = reverse('admin:mikolajki_entry_add')
        self.assertEqual(self.client.get(add).context['adminform'].form.initial['edition'], self.edition.pk)
        data = {'edition': self.edition.pk, 'email': 'Reczny@Example.test', 'full_name': 'Jan Ręczny',
                'shipping_address': 'ul. Długa 1', 'phone': '600100200', 'letter': 'List.'}
        self.assertEqual(self.client.post(add, data).status_code, 302)
        entry = Entry.objects.get()
        self.assertEqual((entry.email, entry.source), ('reczny@example.test', Entry.Source.PANEL))
        self.assertIsNotNone(entry.confirmed_at)
        self.assertIn(entry.code, mail.outbox[-1].body)
        self.assertContains(self.client.post(add, data), 'już zgłoszenie z tym adresem')
        result = self.client.get(reverse('admin:mikolajki_entry_changelist'), {'q': entry.code})
        self.assertEqual(list(result.context['cl'].result_list), [entry])
        Edition.objects.filter(pk=self.edition.pk).update(drawn_at=timezone.now())
        self.assertContains(self.client.post(add, {**data, 'email': 'inny@example.test'}), 'już rozlosowane')
        self.assertFalse(admin.site._registry[Entry].has_delete_permission(
            self.client.get(add).wsgi_request, Entry.objects.get()))
