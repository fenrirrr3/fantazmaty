"""Kalendarz Social Media: dostęp (tylko superuser, koordynatorzy i rola „Social Media”) i prawo zmiany wpisów, siatka miesiąca (granice, luty przestępny, strefa
czasowa Europe/Warsaw i zmiana czasu, błędne parametry), pozycje z listy Social Media z datą, dodawanie, edycja
i usuwanie wpisów z tokenem wersji, ostrzeżenie o zajętym dniu, zakładki i menu."""
import datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import resolve, reverse
from django.utils import timezone
from lxml import html

from core.activity import describe_request
from core.edit_versions import version_of
from core.models import SocialCalendarEntry, SocialMediaEntry, SocialMediaQuote, TextSocialMedia
from core.social_forms import SocialCalendarEntryForm
from core.views.social_calendar import month_start
from core.views.social_media import _token
from fantazmaty.testing import create_member, create_superuser
from people import roles
from texts.models import Anthology, Text

URL = reverse('core:social_calendar')
LIST_URL = reverse('core:social_media')
NOV_5 = datetime.date(2026, 11, 5)


def doc(response):
    return html.fromstring(response.content)


def day_ids(response):
    return [cell.get('id')[len('social-day-'):] for cell in doc(response).xpath('//li[contains(@class, "social-calendar-day")]')]


def cell(response, day):
    found = doc(response).xpath(f'//li[@id="social-day-{day}"]')
    return found[0] if found else None


def item_texts(response, day):
    node = cell(response, day)
    return [' '.join(item.text_content().split()) for item in node.xpath('.//li[contains(@class, "social-calendar-item")]')]


def at(moment):
    """Zamraża „teraz” (UTC) – dzisiejsza data liczy się według strefy Europe/Warsaw."""
    return patch('django.utils.timezone.now', return_value=moment)


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class CalendarDataMixin:
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('calendar-admin')
        cls.coordinator = create_member('calendar-coordinator', roles.PROOFREADING_COORDINATOR)
        cls.social = create_member('calendar-social', roles.SOCIAL_MEDIA)
        cls.other_social = create_member('calendar-social2', roles.SOCIAL_MEDIA)
        anthology = Anthology.objects.create(title='Gotowa', status=Anthology.Status.READY)
        cls.story = Text.objects.create(title='Smocze jajo', anthology=anthology, length=1000)
        cls.entry = SocialCalendarEntry.objects.create(day=NOV_5, description='Zapowiedź naboru', added_by=cls.social,
                                                       added_by_name='calendar-social Testowy')

    def add(self, **data):
        return self.client.post(URL, {'action': 'add', 'day': '2026-11-20', 'description': 'Cytat z „Latarnika”',
                                      'platform': 'facebook', 'month': '2026-11', **data})

    def change(self, action, entry=None, version=None, **data):
        entry = entry or self.entry
        if version is None:
            version = _token(self.client_user, 'calendar', entry.pk, version_of(entry))
        return self.client.post(URL, {'action': action, 'entry': entry.pk, 'version': version, 'day': '2026-11-06',
                                      'description': 'Zmieniony opis', 'platform': 'instagram', 'month': '2026-11',
                                      **data})

    def login(self, user):
        self.client_user = user
        self.client.force_login(user)


class CalendarAccessTests(CalendarDataMixin, TestCase):
    def test_access_matrix(self):
        """Kalendarz (GET) i zapisy (POST) mają tylko superuser, koordynator i rola „Social Media” – widzą formularz
        i przyciski „+”. Redaktor, recenzent, członek bez ról, członek z samą rolą honorową i konto spoza zespołu
        dostają 403: nie dodają wpisu i nie zmieniają ani nie usuwają własnego (np. dodanego przed utratą roli);
        gość – logowanie."""
        no_roles = create_member('calendar-none', roles.EDITOR)
        no_roles.person_profile.roles.clear()
        no_roles.groups.clear()
        cases = (
            ('superuser', self.admin, True), ('koordynator', self.coordinator, True), ('social media', self.social, True),
            ('redaktor', create_member('calendar-editor', roles.EDITOR), False),
            ('recenzent', create_member('calendar-reviewer', roles.REVIEWER), False),
            ('bez ról', no_roles, False), ('rola honorowa', create_member('calendar-emperor', roles.EMPEROR), False),
            ('spoza zespołu', get_user_model().objects.create_user('calendar-outsider'), False),
            ('gość', None, False),
        )
        for label, user, allowed in cases:
            self.client.logout()
            if user:
                self.login(user)
            with self.subTest(user=label, method='GET'):
                page = self.client.get(URL, {'month': '2026-11'})
                self.assertEqual(page.status_code, 200 if allowed else 302 if user is None else 403)
                if allowed:
                    self.assertTrue(doc(page).xpath('//form[contains(@class, "social-calendar-entry-form")]'))
                    self.assertTrue(doc(page).xpath('//a[contains(@class, "social-calendar-add-link")]'))
            own = SocialCalendarEntry.objects.create(day=NOV_5, description=f'Własny: {label}', added_by=user)
            posts = (
                ('add', lambda: self.add(description=f'Wpis: {label}'),
                 lambda: SocialCalendarEntry.objects.filter(description=f'Wpis: {label}').exists()),
                ('edit', lambda: self.change('edit', own, version='' if user is None else None),
                 lambda: SocialCalendarEntry.objects.filter(pk=own.pk, description='Zmieniony opis').exists()),
                ('delete', lambda: self.change('delete', own, version='' if user is None else None),
                 lambda: not SocialCalendarEntry.objects.filter(pk=own.pk).exists()),
            )
            for action, post, saved in posts:
                with self.subTest(user=label, method='POST', action=action):
                    response = post()
                    self.assertEqual(response.status_code, 302 if allowed or user is None else 403)
                    if user is None:
                        self.assertTrue(response['Location'].startswith(reverse('login')))
                    self.assertEqual(saved(), allowed)

    def test_edit_and_delete_by_author_or_coordinator(self):
        """Wpis zmienia i usuwa jego autor (z rolą „Social Media”) albo koordynator / superuser; inna osoba od Social
        Media dostaje 403. Link „Edytuj” i formularz ?edit= widzą tylko uprawnieni."""
        cases = (('autor', self.social, True), ('inna osoba Social Media', self.other_social, False),
                 ('koordynator', self.coordinator, True), ('superuser', self.admin, True))
        for label, user, allowed in cases:
            for action in ('edit', 'delete'):
                with self.subTest(user=label, action=action):
                    entry = SocialCalendarEntry.objects.create(day=NOV_5, description=f'{label} {action}',
                                                               added_by=self.social)
                    self.login(user)
                    page = self.client.get(URL, {'month': '2026-11', 'edit': entry.pk})
                    self.assertEqual(bool(doc(page).xpath(f'//input[@name="entry"][@value="{entry.pk}"]')), allowed)
                    self.assertEqual(f'edit={entry.pk}' in page.content.decode(), allowed)
                    response = self.change(action, entry)
                    self.assertEqual(response.status_code, 302 if allowed else 403)
                    exists = SocialCalendarEntry.objects.filter(pk=entry.pk).exists()
                    self.assertEqual(exists, not (allowed and action == 'delete'))
                    if exists:
                        entry.refresh_from_db()
                        self.assertEqual(entry.description == 'Zmieniony opis', allowed and action == 'edit')

    def test_unknown_action_missing_or_unknown_entry(self):
        """Nieznana operacja i brak wpisu – 400; nieistniejący wpis – 404."""
        self.login(self.social)
        cases = (({'action': 'publish'}, 400), ({'action': 'edit'}, 400), ({'action': 'edit', 'entry': 'x'}, 400),
                 ({'action': 'delete', 'entry': '999999'}, 404))
        for data, status in cases:
            with self.subTest(data=data):
                self.assertEqual(self.client.post(URL, data).status_code, status)


class CalendarGridTests(CalendarDataMixin, TestCase):
    def setUp(self):
        self.login(self.social)

    def test_month_grid_starts_on_monday_and_covers_whole_month(self):
        """Siatka: tygodnie od poniedziałku do niedzieli, z dniami sąsiednich miesięcy; luty przestępny ma 29 dni,
        luty 2027 zaczyna się w poniedziałek i mieści w czterech tygodniach, marzec 2026 ze zmianą czasu ma
        każdy dzień dokładnie raz."""
        cases = (('2026-11', '2026-10-26', '2026-12-06', 30, 'Listopad 2026'),
                 ('2028-02', '2028-01-31', '2028-03-05', 29, 'Luty 2028'),
                 ('2027-02', '2027-02-01', '2027-02-28', 28, 'Luty 2027'),
                 ('2026-03', '2026-02-23', '2026-04-05', 31, 'Marzec 2026'),
                 ('2026-12', '2026-11-30', '2027-01-03', 31, 'Grudzień 2026'))
        for month, first, last, days, title in cases:
            with self.subTest(month=month):
                response = self.client.get(URL, {'month': month})
                ids = day_ids(response)
                self.assertEqual((ids[0], ids[-1]), (first, last))
                self.assertEqual(len(ids) % 7, 0)
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(datetime.date.fromisoformat(ids[0]).weekday(), 0)
                inside = doc(response).xpath('//li[contains(@class, "social-calendar-day")][not(contains(@class, "is-outside"))]')
                self.assertEqual(len(inside), days)
                self.assertEqual(doc(response).xpath('//h2[@id="social-calendar-heading"]/text()'), [title])
        weekdays = doc(self.client.get(URL)).xpath('//ol[@class="social-calendar-weekdays"]/li/text()')
        self.assertEqual(weekdays, ['Pon', 'Wt', 'Śr', 'Czw', 'Pt', 'So', 'Nd'])

    def test_wrong_month_parameter_shows_current_month(self):
        """Brak, śmieci, miesiąc 13 i lata spoza 2000–2100 – bieżący miesiąc (bez błędu)."""
        with at(datetime.datetime(2026, 10, 28, 12, tzinfo=datetime.timezone.utc)):
            self.login(self.social)  # sesja z zamrożonego czasu – wynik nie zależy od daty uruchomienia testów
            for raw in ('', 'abc', '2026-13', '2026-00', '1999-12', '2101-01', '2026-11-05', '<script>'):
                with self.subTest(raw=raw):
                    response = self.client.get(URL, {'month': raw})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(day_ids(response)[0], '2026-09-28')
        self.assertEqual(month_start('2026-1', datetime.date(2026, 5, 5)), datetime.date(2026, 1, 1))

    def test_today_uses_warsaw_time_including_dst_changes(self):
        """„Dziś” to data w Warszawie: 23:30 UTC 31 października (CET) to już 1 listopada, a 23:30 UTC 28 marca –
        29 marca, dzień zmiany czasu. Dzisiejsza komórka jest wyróżniona, a bez parametru widać jej miesiąc."""
        cases = ((datetime.datetime(2026, 10, 31, 23, 30), '2026-11-01'),
                 (datetime.datetime(2026, 3, 28, 23, 30), '2026-03-29'),
                 (datetime.datetime(2026, 3, 29, 21, 59), '2026-03-29'),
                 (datetime.datetime(2026, 10, 25, 22, 59), '2026-10-25'))
        for moment, today in cases:
            with self.subTest(moment=moment), at(moment.replace(tzinfo=datetime.timezone.utc)):
                self.login(self.social)  # sesja z zamrożonego czasu – wynik nie zależy od daty uruchomienia testów
                response = self.client.get(URL)
                today_cells = doc(response).xpath('//li[contains(@class, "is-today")]')
                self.assertEqual([node.get('id') for node in today_cells], [f'social-day-{today}'])
                self.assertEqual(today_cells[0].get('aria-current'), 'date')
                self.assertIn('dziś', today_cells[0].text_content())
                self.assertEqual(doc(response).xpath('//input[@id="social-calendar-day"]/@value'), [today])

    def test_month_navigation_links(self):
        """Poprzedni i następny miesiąc przechodzą przez granicę roku, „Dziś” prowadzi do bieżącego miesiąca."""
        response = self.client.get(URL, {'month': '2027-01'})
        links = {a.text_content().strip(): a.get('href') for a in doc(response).xpath('//nav[@aria-label="Wybór miesiąca"]/a')}
        self.assertEqual(parse_qs(urlsplit(links['‹ Poprzedni']).query), {'month': ['2026-12']})
        self.assertEqual(parse_qs(urlsplit(links['Następny ›']).query), {'month': ['2027-02']})
        self.assertEqual(urlsplit(links['Dziś']).query, '')
        self.assertEqual(doc(response).xpath('//input[@name="month"][@type="month"]/@value'), ['2027-01'])

    def test_empty_month(self):
        """Miesiąc bez wpisów – komunikat „Brak wpisów w tym miesiącu.” i żadnej pozycji w komórkach."""
        response = self.client.get(URL, {'month': '2030-05'})
        self.assertContains(response, 'Brak wpisów w tym miesiącu.')
        self.assertFalse(doc(response).xpath('//li[contains(@class, "social-calendar-item")]'))


class CalendarListItemsTests(CalendarDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        TextSocialMedia.objects.create(text=cls.story, status='in_progress', posted_on=NOV_5)
        cls.other = SocialMediaEntry.objects.create(description='Premiera antologii', status='posted', posted_on=NOV_5)
        SocialMediaEntry.objects.create(description='Bez daty')
        cls.quote = SocialMediaQuote.objects.create(text=cls.story, content='Smok spał.', status='in_progress',
                                                    posted_on=datetime.date(2026, 11, 6))
        SocialMediaQuote.objects.create(text=cls.story, content='Wolny cytat', posted_on=datetime.date(2026, 11, 6))

    def test_calendar_shows_dated_list_items(self):
        """Kalendarz łączy wpisy kalendarza z pozycjami listy, które mają datę: plan, opowiadanie i „Inne” z datą
        wrzucenia oraz cytat w przygotowaniu – u autora planu z linkiem „Edytuj”, u innej osoby od Social Media bez
        niego. Cytat „Wolne” (data dodania) i wpis bez daty nie trafiają do kalendarza."""
        plan = 'Plan Zapowiedź naboru Facebook · calendar-social Testowy'
        others = ['Opowiadanie „Smocze jajo” W przygotowaniu', 'Inne Premiera antologii Wrzucone']
        cases = ((self.social, [f'{plan} Edytuj wpis „Zapowiedź naboru”', *others]), (self.other_social, [plan, *others]))
        day6, summary = ['Cytat Cytat z „Smocze jajo” W przygotowaniu'], '4 wpisy w tym miesiącu.'
        for user, day5 in cases:
            with self.subTest(user=user.username):
                self.login(user)
                response = self.client.get(URL, {'month': '2026-11'})
                self.assertEqual(item_texts(response, '2026-11-05'), day5)
                self.assertEqual(item_texts(response, '2026-11-06'), day6)
                self.assertContains(response, summary)
                self.assertNotContains(response, 'Wolny cytat')
                self.assertNotContains(response, 'Bez daty')

    def test_list_items_link_to_their_rows_and_list_opens_section(self):
        """Pozycje prowadzą do wiersza na liście: opowiadanie i cytat – wyszukanie po tytule (z tekstami
        niewydanymi; cytaty z rozwiniętą sekcją), „Inne” – ?open=other, które rozwija sekcję „Inne”."""
        self.login(self.social)
        response = self.client.get(URL, {'month': '2026-11'})
        links = doc(response).xpath('//a[contains(@class, "social-calendar-title")]/@href')
        expected = {f'social-text-{self.story.pk}': {'q': ['Smocze jajo'], 'unpublished': ['1']},
                    f'social-entry-{self.other.pk}': {'open': ['other']},
                    f'social-quote-text-{self.story.pk}': {'q': ['Smocze jajo'], 'unpublished': ['1'], 'quotes_page': ['1']}}
        found = {urlsplit(link).fragment: parse_qs(urlsplit(link).query) for link in links}
        self.assertEqual(found, expected)
        for link in links:
            self.assertEqual(urlsplit(link).path, LIST_URL)
        for params, opened in (({}, False), ({'open': 'other'}, True)):
            with self.subTest(params=params):
                page = doc(self.client.get(LIST_URL, params))
                self.assertEqual(page.xpath('//details[@id="social-other"]')[0].get('open') is not None, opened)
                if opened:
                    self.assertTrue(page.xpath(f'//tr[@id="social-entry-{self.other.pk}"]'))

    def test_busy_day_counter_and_warning_before_adding(self):
        """Zajęty dzień ma licznik pozycji; wybór dnia („+”, ?day=) wypełnia datę formularza i pokazuje ostrzeżenie
        z listą zaplanowanych pozycji – na wolny dzień bez ostrzeżenia."""
        self.login(self.social)
        response = self.client.get(URL, {'month': '2026-11'})
        busy = cell(response, '2026-11-05')
        self.assertIn('is-busy', busy.get('class'))
        self.assertEqual(busy.xpath('.//span[@class="record-counter"]')[0].text_content().strip(), '3 – 3 wpisy')
        self.assertNotIn('is-busy', cell(response, '2026-11-07').get('class'))
        add_link = busy.xpath('.//a[contains(@class, "social-calendar-add-link")]/@href')[0]
        self.assertEqual(parse_qs(urlsplit(add_link).query), {'month': ['2026-11'], 'day': ['2026-11-05']})
        page = self.client.get(add_link)
        self.assertEqual(doc(page).xpath('//input[@id="social-calendar-day"]/@value'), ['2026-11-05'])
        self.assertIn('is-selected', cell(page, '2026-11-05').get('class'))
        warning = ' '.join(doc(page).xpath('//div[contains(@class, "social-calendar-busy")]')[0].text_content().split())
        self.assertIn('5 listopada 2026 – są już wpisy (3)', warning)
        self.assertIn('Plan: Zapowiedź naboru (calendar-social Testowy)', warning)
        self.assertIn('Inne: Premiera antologii', warning)
        free = self.client.get(URL, {'day': '2026-11-07'})
        self.assertFalse(doc(free).xpath('//div[contains(@class, "social-calendar-busy")]'))
        self.assertEqual(day_ids(free)[0], '2026-10-26')  # dzień bez miesiąca otwiera swój miesiąc


class CalendarSaveTests(CalendarDataMixin, TestCase):
    def setUp(self):
        self.login(self.social)

    def test_add_saves_author_and_returns_to_day(self):
        """Nowy wpis zapisuje autora (konto i podpis) i wraca do miesiąca wpisu przewiniętego do dnia; opis bez
        nadmiarowych spacji. Wpis na wolny dzień – bez ostrzeżenia."""
        response = self.add(description='  Cytat   z „Latarnika”  ', day='2026-12-01', platform='instagram')
        entry = SocialCalendarEntry.objects.get(day=datetime.date(2026, 12, 1))
        self.assertRedirects(response, f'{URL}?month=2026-12#social-day-2026-12-01', fetch_redirect_response=False)
        self.assertEqual((entry.description, entry.platform, entry.added_by, entry.added_by_name, entry.updated_by),
                         ('Cytat z „Latarnika”', 'instagram', self.social, 'calendar-social Testowy', self.social))
        self.assertEqual(messages_of(response), ['Dodano wpis na 1 grudnia 2026.'])

    def test_adding_to_busy_day_warns_without_blocking(self):
        """Wpis na dzień, który ma już pozycje, zapisuje się, a komunikat ostrzega, ile ich jest."""
        response = self.add(day='2026-11-05')
        self.assertEqual(SocialCalendarEntry.objects.filter(day=NOV_5).count(), 2)
        self.assertEqual(messages_of(response), [
            'Dodano wpis na 5 listopada 2026.',
            'Na 5 listopada 2026 zaplanowano już 1 wpis – sprawdź w kalendarzu, czy wpisy sobie nie przeszkadzają.'])

    def test_validation_errors(self):
        """Błędne pola: komunikaty po polsku przy polach, status 400, nic nie zapisane, wpisane dane zostają."""
        cases = (({'description': '   '}, 'Wpisz, co idzie tego dnia.'),
                 ({'description': 'x' * 301}, 'Opis może mieć najwyżej 300 znaków.'),
                 ({'day': ''}, 'Podaj dzień.'), ({'day': '2026-02-30'}, 'Podaj prawidłową datę.'),
                 ({'day': '1999-12-31'}, 'Podaj dzień z lat 2000–2100.'),
                 ({'platform': 'tiktok'}, 'Wybierz, gdzie idzie wpis.'))
        for data, error in cases:
            with self.subTest(data=data):
                response = self.add(**data)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, error, status_code=400)
                self.assertContains(response, 'Nie dodano wpisu – popraw zaznaczone pola.', status_code=400)
                self.assertEqual(SocialCalendarEntry.objects.count(), 1)
        response = self.add(description='Zostaje', day='')
        self.assertEqual(doc(response).xpath('//input[@id="social-calendar-description"]/@value'), ['Zostaje'])
        # Formularz zachowuje formaty daty z innych pól Social Media.
        self.assertTrue(SocialCalendarEntryForm({'day': '05.11.2026', 'description': 'x', 'platform': 'other'}).is_valid())

    def test_edit_saves_without_changing_author(self):
        """Zapis zmiany: nowe pola, autor bez zmian, „zmienił(a)” – osoba zapisująca; powrót do nowego dnia."""
        self.login(self.coordinator)
        response = self.change('edit')
        self.assertRedirects(response, f'{URL}?month=2026-11#social-day-2026-11-06', fetch_redirect_response=False)
        self.entry.refresh_from_db()
        self.assertEqual((self.entry.day, self.entry.description, self.entry.platform, self.entry.added_by,
                          self.entry.updated_by),
                         (datetime.date(2026, 11, 6), 'Zmieniony opis', 'instagram', self.social, self.coordinator))
        invalid = self.change('edit', description='')
        self.assertContains(invalid, 'Wpisz, co idzie tego dnia.', status_code=400)
        self.assertEqual(doc(invalid).xpath('//input[@name="entry"]/@value'), [str(self.entry.pk)])

    def test_stale_form_does_not_overwrite_or_delete(self):
        """Nieaktualny token wersji (ktoś zmienił wpis w międzyczasie): zapis – 409 z opisem obecnego stanu
        i świeżym tokenem, usunięcie – komunikat bez usuwania; podrobiony token tak samo."""
        stale = _token(self.social, 'calendar', self.entry.pk, version_of(self.entry))
        SocialCalendarEntry.objects.filter(pk=self.entry.pk).update(description='Cudza zmiana')
        self.entry.refresh_from_db()
        self.entry.save()  # podbija wersję jak zapis z innej karty
        for version in (stale, 'podrobiony'):
            with self.subTest(version=version[:10]):
                response = self.change('edit', version=version)
                self.assertEqual(response.status_code, 409)
                self.assertContains(response, 'Ktoś zmienił ten wpis w międzyczasie (obecnie: 5 listopada 2026, '
                                              'Facebook: Cudza zmiana)', status_code=409)
                fresh = doc(response).xpath('//input[@name="version"]/@value')[0]
                self.assertNotEqual(fresh, stale)
                deleted = self.change('delete', version=version)
                self.assertEqual(messages_of(deleted)[-1],
                                 'Nie usunięto wpisu „Cudza zmiana” – ktoś zmienił go w międzyczasie.')
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.description, 'Cudza zmiana')
        self.assertEqual(self.change('edit', version=fresh).status_code, 302)

    def test_delete(self):
        """Usunięcie z formularza zmiany: wpis znika, komunikat i powrót do dnia."""
        response = self.change('delete')
        self.assertRedirects(response, f'{URL}?month=2026-11#social-day-2026-11-05', fetch_redirect_response=False)
        self.assertFalse(SocialCalendarEntry.objects.exists())
        self.assertEqual(messages_of(response), ['Usunięto wpis „Zapowiedź naboru”.'])

    def test_edit_form_page(self):
        """?edit=<id>: formularz „Zmień wpis” z danymi wpisu, przyciskami Zapisz / Usuń (z pytaniem) / Anuluj;
        ostrzeżenie o zajętym dniu nie liczy edytowanego wpisu."""
        page = doc(self.client.get(URL, {'month': '2026-11', 'edit': self.entry.pk}))
        self.assertEqual(page.xpath('//h2[@id="social-calendar-form-heading"]/text()'), ['Zmień wpis'])
        self.assertEqual(page.xpath('//input[@id="social-calendar-description"]/@value'), ['Zapowiedź naboru'])
        self.assertEqual(page.xpath('//input[@id="social-calendar-day"]/@value'), ['2026-11-05'])
        self.assertEqual(page.xpath('//button[@value="delete"]/@data-confirm'), ['Usunąć wpis „Zapowiedź naboru”?'])
        self.assertFalse(page.xpath('//div[contains(@class, "social-calendar-busy")]'))
        for raw in ('abc', '999999'):
            with self.subTest(edit=raw):
                other = doc(self.client.get(URL, {'month': '2026-11', 'edit': raw}))
                self.assertEqual(other.xpath('//h2[@id="social-calendar-form-heading"]/text()'), ['Zaplanuj wpis'])


class CalendarNavigationTests(CalendarDataMixin, TestCase):
    def test_tabs_on_both_pages_and_menu_link(self):
        """Lista i Kalendarz mają zakładki „Lista / Kalendarz” z zaznaczoną bieżącą; menu „Promocja” ma pod
        „Social Media” pozycję „Kalendarz Social Media”, zaznaczoną na stronie kalendarza."""
        for user in (self.coordinator, self.social):
            self.login(user)
            for url, current in ((LIST_URL, 'Lista'), (URL, 'Kalendarz')):
                with self.subTest(user=user.username, url=url):
                    page = doc(self.client.get(url))
                    tabs = page.xpath('//nav[@aria-label="Widok Social Media"]/a')
                    self.assertEqual([(tab.text_content(), tab.get('href')) for tab in tabs],
                                     [('Lista', LIST_URL), ('Kalendarz', URL)])
                    self.assertEqual([tab.text_content() for tab in tabs if tab.get('aria-current') == 'page'], [current])
                    menu = page.xpath('//details[@data-menu-section="promotion"]/a')
                    self.assertEqual([link.text_content() for link in menu], ['Social Media', 'Kalendarz Social Media'])
                    self.assertEqual([link.text_content() for link in menu if link.get('aria-current') == 'page'],
                                     ['Social Media' if url == LIST_URL else 'Kalendarz Social Media'])

    def test_activity_labels(self):
        """Dziennik aktywności: wizyta – „Kalendarz Social Media”, zapis – „Zapis wpisu w kalendarzu Social Media”."""
        match = resolve(URL)
        self.assertEqual(describe_request(match, 'GET')[0], 'Kalendarz Social Media')
        self.assertEqual(describe_request(match, 'POST')[0], 'Zapis wpisu w kalendarzu Social Media')


class CalendarModelTests(TestCase):
    def test_str_ordering_and_admin_list(self):
        """Wpis opisuje się datą i treścią; kolejność: dzień, potem czas dodania. Panel admina (superuser) listuje
        wpisy."""
        later = SocialCalendarEntry.objects.create(day=datetime.date(2026, 11, 6), description='Drugi')
        first = SocialCalendarEntry.objects.create(day=datetime.date(2026, 11, 5), description='Pierwszy')
        same_day = SocialCalendarEntry.objects.create(day=datetime.date(2026, 11, 5), description='Trzeci')
        self.assertEqual(list(SocialCalendarEntry.objects.all()), [first, same_day, later])
        self.assertEqual(str(first), '05.11.2026 – Pierwszy')
        self.assertEqual(first.get_platform_display(), 'Facebook')
        self.client.force_login(create_superuser('calendar-model-admin'))
        self.assertContains(self.client.get(reverse('admin:core_socialcalendarentry_changelist')), 'Pierwszy')
        self.assertTrue(timezone.is_aware(first.created_at))
