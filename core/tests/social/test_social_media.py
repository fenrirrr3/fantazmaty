"""Zakładka Social Media: dostęp (tylko superuser, koordynatorzy i rola „Social Media”), zakres opowiadań (antologie gotowe /
niewydane), kolumny, zapis wierszy z tokenem wersji (bez nadpisywania cudzych zmian), sekcje „Inne” i „Cytaty”,
panel admina i migracja danych roli."""
import datetime
import importlib
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.edit_versions import version_of
from core.models import SocialMediaEntry, SocialMediaQuote, TextSocialMedia
from core.views.social_media import _token
from fantazmaty.testing import create_member, create_superuser
from people import roles
from people.models import Role
from texts.models import Anthology, ForeignAuthor, Text, TextTranslation
from workflow.models import WorkflowStage

URL = reverse('core:social_media')


def page_doc(response):
    return html.fromstring(response.content)


def table_titles(response):
    return page_doc(response).xpath('//section[contains(@class, "social-media-card")]//tbody/tr/td[2]//strong/text()')


def quote_titles(response):
    return page_doc(response).xpath('//details[@id="social-quotes"]//tr[@class="social-quote-text"]/td[2]//strong/text()')


class SocialMediaDataMixin:
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('social-admin')
        cls.coordinator = create_member('social-coordinator', roles.PROOFREADING_COORDINATOR)
        cls.social = create_member('social-person', roles.SOCIAL_MEDIA)
        cls.ready = Anthology.objects.create(title='Gotowa antologia', status=Anthology.Status.READY)
        cls.preparing = Anthology.objects.create(title='Antologia w przygotowaniu')
        cls.abandoned = Anthology.objects.create(title='Porzucona antologia', status=Anthology.Status.ABANDONED)
        cls.translated = Anthology.objects.create(title='Przekłady', status=Anthology.Status.READY, is_translated=True)
        cls.novel = Anthology.objects.create(title='Powieść', is_novel=True)
        cls.story = Text.objects.create(title='Smocze jajo', anthology=cls.ready, length=1000,
                                        tags='smoki, magia', genre='fantasy')
        cls.story.authors.add(Author.objects.create(first_name='Jan', last_name='Kowalski'),
                              Author.objects.create(first_name='Anna', last_name='Nowak', pseudonym='A. N. Pióro'))
        cls.draft = Text.objects.create(title='Szkic', anthology=cls.preparing, length=1000)
        Text.objects.create(title='Porzucony', anthology=cls.abandoned, length=1000)
        withdrawn = Text.objects.create(title='Wycofany', anthology=cls.ready, length=1000)
        WorkflowStage.objects.create(text=withdrawn, stage_type='withdrawn', is_released=True, is_completed=False)
        cls.foreign = Text.objects.create(title='Obcy', anthology=cls.translated, length=1000, genre='SF')
        TextTranslation.objects.get_or_create(text=cls.foreign)[0].foreign_authors.add(
            ForeignAuthor.objects.create(first_name='Ursula', last_name='Le Guin', pseudonym='U. K. Le Guin'))
        Text.objects.create(chapter_number=1, anthology=cls.novel)

    def row_version(self, response, form_id):
        return page_doc(response).xpath(f'//form[@id="{form_id}"]/input[@name="version"]/@value')[0]

    def save_text(self, text, version, **data):
        return self.client.post(URL, {'action': 'text', 'text': text.pk, 'version': version,
                                      'status': 'free', 'posted_on': '', 'url': '', **data})


class SocialMediaAccessTests(SocialMediaDataMixin, TestCase):
    def test_access_matrix(self):
        """Stronę (GET) i zapisy (POST) mają tylko superuser, koordynator dowolnego działu i rola „Social Media”.
        Redaktor, recenzent, członek bez ról, członek z samą rolą honorową i konto spoza zespołu dostają 403
        i niczego nie zapisują – także cytatu ani usunięcia własnego cytatu „Wolne”; gość – logowanie."""
        no_roles = create_member('social-none', roles.EDITOR)
        no_roles.person_profile.roles.clear()
        no_roles.groups.clear()
        cases = (
            ('superuser', self.admin, True), ('koordynator', self.coordinator, True),
            ('social media', self.social, True),
            ('redaktor', create_member('social-editor', roles.EDITOR), False),
            ('recenzent', create_member('social-reviewer', roles.REVIEWER), False),
            ('bez ról', no_roles, False), ('rola honorowa', create_member('social-emperor', roles.EMPEROR), False),
            ('spoza zespołu', get_user_model().objects.create_user('social-outsider'), False),
            ('gość', None, False),
        )
        for label, user, allowed in cases:
            self.client.logout()
            if user:
                self.client.force_login(user)
            own = SocialMediaQuote.objects.create(text=self.story, content=f'Własny: {label}', added_by=user)
            posts = (
                ('entry_add', {'description': f'Wpis: {label}', 'status': 'free'},
                 lambda: SocialMediaEntry.objects.filter(description=f'Wpis: {label}').exists()),
                ('quote_add', {'text': self.story.pk, 'content': f'Cytat: {label}'},
                 lambda: SocialMediaQuote.objects.filter(content=f'Cytat: {label}').exists()),
                ('quote_delete', {'quote': own.pk, 'version': _token(user, 'quote', own.pk, version_of(own))
                                  if user else ''},
                 lambda: not SocialMediaQuote.objects.filter(pk=own.pk).exists()),
            )
            with self.subTest(user=label, method='GET'):
                response = self.client.get(URL)
                self.assertEqual(response.status_code, 200 if allowed else 302 if user is None else 403)
            for action, data, saved in posts:
                with self.subTest(user=label, method='POST', action=action):
                    response = self.client.post(URL, {'action': action, **data})
                    self.assertEqual(response.status_code, 302 if allowed or user is None else 403)
                    if user is None:
                        self.assertTrue(response['Location'].startswith(reverse('login')))
                    self.assertEqual(saved(), allowed)


class SocialMediaTextListTests(SocialMediaDataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.social)

    def test_scope_ready_anthologies_by_default_unpublished_on_request(self):
        """Domyślnie tylko antologie „Gotowe”; z polem „Pokaż teksty niewydane” także antologie
        w przygotowaniu. Teksty wycofane, antologie porzucone i rozdziały powieści nie pojawiają się nigdy."""
        for params, expected in (({}, ['Obcy', 'Smocze jajo']), ({'unpublished': '1'}, ['Obcy', 'Smocze jajo', 'Szkic'])):
            with self.subTest(params=params):
                response = self.client.get(URL, {'sort': 'title', **params})
                self.assertEqual(table_titles(response), expected)
                checkbox = page_doc(response).xpath('//input[@name="unpublished"]')[0]
                self.assertEqual(checkbox.get('checked') is not None, bool(params))

    def test_columns_show_signatures_translation_authors_tags_and_genre(self):
        """Wiersz pokazuje antologię, tytuł, autorów po przecinku (pseudonim zamiast nazwiska; w tłumaczeniu –
        autorów zagranicznych), tagi, gatunek i status „Wolne” dla tekstu bez zapisanego wpisu."""
        doc = page_doc(self.client.get(URL, {'sort': 'title'}))
        cells = {row.get('id'): [' '.join(cell.text_content().split()) for cell in row.xpath('./td')[:6]]
                 for row in doc.xpath('//tbody/tr[starts-with(@id, "social-text-")]')}
        self.assertEqual(cells[f'social-text-{self.story.pk}'][:5],
                         ['Gotowa antologia', 'Smocze jajo', 'Jan Kowalski, A. N. Pióro', 'smoki, magia', 'fantasy'])
        self.assertEqual(cells[f'social-text-{self.foreign.pk}'][:5], ['Przekłady', 'Obcy', 'U. K. Le Guin', '–', 'SF'])
        self.assertEqual(doc.xpath(f'//select[@id="social-text-{self.story.pk}-status"]/option[@selected]/text()'),
                         ['Wolne'])
        self.assertFalse(TextSocialMedia.objects.exists())

    def test_search_status_and_anthology_filters(self):
        """Wyszukiwarka obejmuje tytuł, podpis autora i tagi; filtr statusu „Wolne” obejmuje teksty bez wpisu."""
        TextSocialMedia.objects.create(text=self.foreign, status='posted')
        cases = (
            ({'q': 'Pióro'}, ['Smocze jajo']), ({'q': 'Le Guin'}, ['Obcy']), ({'q': 'smoki'}, ['Smocze jajo']),
            ({'status': 'free'}, ['Smocze jajo']), ({'status': 'posted'}, ['Obcy']),
            ({'status': ['free', 'posted']}, ['Obcy', 'Smocze jajo']), ({'status': 'zly'}, []),
            ({'anthology': self.translated.pk}, ['Obcy']), ({'anthology': self.preparing.pk}, []),
            ({'anthology': self.preparing.pk, 'unpublished': '1'}, ['Szkic']),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(table_titles(self.client.get(URL, {'sort': 'title', **params})), expected)

    def test_query_count_does_not_grow_with_rows(self):
        """Liczba zapytań strony jest taka sama dla 1 i 10 opowiadań (autorzy, wpisy, cytaty z dodającymi i wersje
        pobierane zbiorczo)."""
        def count():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(URL, {'anthology': book.pk}).status_code, 200)
            return len(queries)

        book = Anthology.objects.create(title='Licznik', status=Anthology.Status.READY)
        author = Author.objects.create(first_name='Ewa', last_name='Licz')
        SocialMediaEntry.objects.create(description='Inny wpis')

        def add(index):
            text = Text.objects.create(title=f'Licznik {index}', anthology=book, length=100, tags='a')
            text.authors.add(author)
            if index % 2:
                TextSocialMedia.objects.create(text=text, status='in_progress', url='https://example.com/')
                SocialMediaQuote.objects.create(text=text, content=f'Cytat {index}', added_by=self.coordinator)
        add(1)
        self.client.get(URL)  # pierwsze wejście zapisuje sesję (dziennik aktywności) – poza pomiarem
        single = count()
        for index in range(2, 11):
            add(index)
        self.assertEqual(count(), single)


class SocialMediaTextSaveTests(SocialMediaDataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.social)

    def test_first_save_creates_record_lazily_then_updates_it(self):
        """Pierwszy zapis tworzy wpis tekstu (wcześniej go nie ma), kolejny go aktualizuje; po zapisie wraca na
        tę samą listę (filtry, sortowanie, strona) z kotwicą do wiersza, a zapisany link jest klikalny."""
        query = 'unpublished=1&sort=-title&page=1&evil=https%3A%2F%2Fexample.com'
        page = self.client.get(f'{URL}?{query}')
        response = self.client.post(URL, {
            'action': 'text', 'text': self.story.pk, 'version': self.row_version(page, f'social-text-form-{self.story.pk}'),
            'status': 'posted', 'posted_on': '2026-10-01', 'url': 'https://facebook.com/fantazmaty/1', 'return_query': query})
        location = urlsplit(response['Location'])
        self.assertEqual((location.path, location.query, location.fragment),
                         (URL, 'unpublished=1&sort=-title&page=1', f'social-text-{self.story.pk}'))
        record = TextSocialMedia.objects.get(text=self.story)
        self.assertEqual((record.status, record.posted_on, record.url, record.updated_by),
                         ('posted', datetime.date(2026, 10, 1), 'https://facebook.com/fantazmaty/1', self.social))
        page = self.client.get(URL)
        row = page_doc(page).xpath(f'//tr[@id="social-text-{self.story.pk}"]')[0]
        self.assertEqual(row.xpath('.//a[@class="social-media-link"]/@href'), ['https://facebook.com/fantazmaty/1'])
        self.assertEqual(row.xpath('.//select[@name="status"]/option[@selected]/text()'), ['Wrzucone'])
        self.assertFalse(row.xpath('.//span[contains(@class, "status")]'))  # bez osobnej etykiety statusu
        future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        response = self.save_text(self.story, self.row_version(page, f'social-text-form-{self.story.pk}'),
                                  status='in_progress', posted_on=future)
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual((record.status, record.posted_on.isoformat(), record.url), ('in_progress', future, ''))
        self.assertEqual(TextSocialMedia.objects.count(), 1)

    def test_invalid_link_or_text_outside_the_table_is_rejected(self):
        """Link musi być adresem http(s); tekst spoza tabeli (np. wycofany) nie dostaje wpisu."""
        version = self.row_version(self.client.get(URL), f'social-text-form-{self.story.pk}')
        for value in ('nie link', 'ftp://example.com/plik', 'javascript:alert(1)'):
            with self.subTest(url=value):
                response = self.save_text(self.story, version, url=value)
                self.assertEqual(response.status_code, 400)
                row = page_doc(response).xpath(f'//tr[@id="social-text-{self.story.pk}"]')[0]
                self.assertIn('http', ' '.join(row.xpath('.//span[@class="field-error"]/text()')))
                self.assertEqual(row.xpath('.//input[@name="url"]/@value'), [value])
        withdrawn = Text.objects.get(title='Wycofany')
        self.assertEqual(self.save_text(withdrawn, version, status='posted').status_code, 404)
        self.assertFalse(TextSocialMedia.objects.exists())

    def test_stale_form_does_not_overwrite_another_persons_change(self):
        """Dwie osoby otwierają listę; zapis drugiej z nieaktualnym formularzem kończy się komunikatem (409)
        z aktualnymi danymi i nie nadpisuje zmiany – także gdy wpis tekstu dopiero powstał."""
        stale = self.row_version(self.client.get(URL), f'social-text-form-{self.story.pk}')
        self.client.force_login(self.coordinator)
        fresh = self.row_version(self.client.get(URL), f'social-text-form-{self.story.pk}')
        self.assertEqual(self.save_text(self.story, fresh, status='posted', url='https://x.com/1').status_code, 302)
        self.client.force_login(self.social)
        response = self.save_text(self.story, stale, status='in_progress')
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'Ktoś zmienił ten wpis w międzyczasie (obecnie: Wrzucone, https://x.com/1)',
                            status_code=409)
        record = TextSocialMedia.objects.get(text=self.story)
        self.assertEqual((record.status, record.url, record.updated_by), ('posted', 'https://x.com/1', self.coordinator))
        # Po obejrzeniu konfliktu formularz ma aktualny token – ponowny zapis przechodzi.
        retry = self.row_version(response, f'social-text-form-{self.story.pk}')
        self.assertEqual(self.save_text(self.story, retry, status='in_progress').status_code, 302)
        self.assertEqual(self.save_text(self.story, 'zmyślony', status='free').status_code, 409)


class SocialMediaOtherEntriesTests(SocialMediaDataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.coordinator)

    def other_section(self, response):
        return page_doc(response).xpath('//details[@id="social-other"]')[0]

    def test_add_edit_delete_and_section_state(self):
        """„Inne”: pierwsza sekcja (potem „Cytaty” i „Opowiadania”), domyślnie zwinięta; dodanie, edycja i usunięcie wpisu wracają z rozwiniętą sekcją
        (tylko raz – następne wejście znów ją zwija); usuwanie ma pytanie potwierdzające."""
        page = self.client.get(URL)
        self.assertIsNone(self.other_section(page).get('open'))
        self.assertEqual([el.get('id') or el.get('aria-labelledby') for el in
                          page_doc(page).xpath('//details[starts-with(@id, "social-")] | //section[contains(@class, "social-media-card")]')],
                         ['social-other', 'social-quotes', 'social-texts-heading'])
        response = self.client.post(URL, {'action': 'entry_add', 'description': '  Zapowiedź   naboru ',
                                          'status': 'in_progress', 'posted_on': '2026-11-01', 'url': ''}, follow=True)
        entry = SocialMediaEntry.objects.get()
        self.assertEqual((entry.description, entry.status, entry.posted_on, entry.updated_by),
                         ('Zapowiedź naboru', 'in_progress', datetime.date(2026, 11, 1), self.coordinator))
        self.assertEqual(response.redirect_chain[-1][0], f'{URL}#social-entry-{entry.pk}')
        self.assertIsNotNone(self.other_section(response).get('open'))
        self.assertIsNone(self.other_section(self.client.get(URL)).get('open'))
        page = self.client.get(URL)
        version = self.row_version(page, f'social-entry-form-{entry.pk}')
        self.assertEqual(page_doc(page).xpath(f'//form[@id="social-entry-form-{entry.pk}"]/button[@value="entry_delete"]'
                                              '/@data-confirm'), ['Usunąć wpis „Zapowiedź naboru”?'])
        response = self.client.post(URL, {'action': 'entry_edit', 'entry': entry.pk, 'version': version,
                                          'description': 'Nabór otwarty', 'status': 'posted', 'posted_on': '2026-11-02',
                                          'url': 'https://instagram.com/p/1'}, follow=True)
        entry.refresh_from_db()
        self.assertEqual((entry.description, entry.status, entry.url), ('Nabór otwarty', 'posted', 'https://instagram.com/p/1'))
        self.assertIsNotNone(self.other_section(response).get('open'))
        version = self.row_version(response, f'social-entry-form-{entry.pk}')
        response = self.client.post(URL, {'action': 'entry_delete', 'entry': entry.pk, 'version': version}, follow=True)
        self.assertFalse(SocialMediaEntry.objects.exists())
        self.assertContains(response, 'Usunięto wpis „Nabór otwarty”.')

    def test_validation_errors_keep_the_section_open(self):
        """Błędny formularz „Inne” (brak lub zbyt długi opis, zły link) nie zapisuje wpisu, pokazuje błąd
        i zostawia sekcję rozwiniętą z wpisanymi danymi."""
        cases = (
            ({'description': '   '}, 'Wpisz krótki opis.'),
            ({'description': 'x' * 201}, 'Krótki opis może mieć najwyżej 200 znaków.'),
            ({'description': 'Opis', 'url': 'ftp://example.com'}, 'Podaj prawidłowy link'),
            ({'description': 'Opis', 'status': 'inny'}, 'Wybierz poprawną wartość'),
        )
        for data, error in cases:
            with self.subTest(data=data):
                response = self.client.post(URL, {'action': 'entry_add', 'status': 'free', **data})
                self.assertEqual(response.status_code, 400)
                self.assertIsNotNone(self.other_section(response).get('open'))
                self.assertIn(error, self.other_section(response).text_content())
        self.assertFalse(SocialMediaEntry.objects.exists())
        entry = SocialMediaEntry.objects.create(description='Stary')
        version = self.row_version(self.client.get(URL), f'social-entry-form-{entry.pk}')
        response = self.client.post(URL, {'action': 'entry_edit', 'entry': entry.pk, 'version': version,
                                          'description': '', 'status': 'free'})
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(self.other_section(response).get('open'))
        entry.refresh_from_db()
        self.assertEqual(entry.description, 'Stary')

    def test_stale_entry_form_is_rejected(self):
        """Edycja lub usunięcie wpisu „Inne” z nieaktualnym formularzem nie zmienia wpisu zmienionego przez inną osobę."""
        entry = SocialMediaEntry.objects.create(description='Premiera')
        stale = self.row_version(self.client.get(URL), f'social-entry-form-{entry.pk}')
        SocialMediaEntry.objects.filter(pk=entry.pk).update(status='posted')
        for action, expected in (('entry_edit', 409), ('entry_delete', 302)):
            with self.subTest(action=action):
                response = self.client.post(URL, {'action': action, 'entry': entry.pk, 'version': stale,
                                                  'description': 'Zmieniona', 'status': 'free'})
                self.assertEqual(response.status_code, expected)
                entry.refresh_from_db()
                self.assertEqual((entry.description, entry.status), ('Premiera', 'posted'))
        self.assertEqual(self.client.post(URL, {'action': 'nieznana'}).status_code, 400)

    def test_missing_wrong_or_unknown_row_is_rejected(self):
        """Zapis bez identyfikatora wiersza lub z błędnym – 400, nieistniejący wpis „Inne” – 404; nic się nie zmienia."""
        cases = (
            ('tekst bez id', {'action': 'text'}, 400), ('tekst z id 0', {'action': 'text', 'text': '0'}, 400),
            ('tekst z id nieliczbowym', {'action': 'text', 'text': '1a'}, 400),
            ('wpis bez id', {'action': 'entry_edit'}, 400), ('wpis z id ujemnym', {'action': 'entry_delete', 'entry': '-1'}, 400),
            ('nieistniejący wpis', {'action': 'entry_edit', 'entry': '999999'}, 404),
        )
        for label, data, status in cases:
            with self.subTest(label):
                response = self.client.post(URL, {'version': 'x', 'description': 'Opis', 'status': 'posted', **data})
                self.assertEqual(response.status_code, status)
        self.assertFalse(TextSocialMedia.objects.exists() or SocialMediaEntry.objects.exists())

    def test_records_are_described_by_text_status_or_description(self):
        """Opis rekordów w panelu admina: tekst i status, krótki opis wpisu „Inne” albo tekst i początek cytatu."""
        cases = ((TextSocialMedia(text=self.story, status='posted'), 'Smocze jajo – Wrzucone'),
                 (SocialMediaEntry(description='Zapowiedź naboru'), 'Zapowiedź naboru'),
                 (SocialMediaQuote(text=self.story, content='Krótki\ncytat'), 'Smocze jajo – Krótki cytat'),
                 (SocialMediaQuote(text=self.story, content='x' * 61), f'Smocze jajo – {"x" * 60}…'))
        for record, expected in cases:
            with self.subTest(model=type(record).__name__):
                self.assertEqual(str(record), expected)


class SocialMediaQuoteTests(SocialMediaDataMixin, TestCase):
    """Sekcja „Cytaty” (osoby od Social Media): dodawanie, status i data, usuwanie, walidacja i wersje."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = create_member('social-other', roles.SOCIAL_MEDIA)

    def quotes_section(self, response):
        return page_doc(response).xpath('//details[@id="social-quotes"]')[0]

    def add_quote(self, text, content, **extra):
        return self.client.post(URL, {'action': 'quote_add', 'text': text.pk, 'content': content, **extra})

    def change_quote(self, user, quote, action, **data):
        return self.client.post(URL, {'action': action, 'quote': quote.pk,
                                      'version': _token(user, 'quote', quote.pk, version_of(quote)), **data})

    def test_section_collapsed_by_default_and_lists_same_texts_as_stories(self):
        """Dla Social Media „Cytaty” są domyślnie zwinięte (otwiera je parametr strony cytatów); lista tekstów ma zakres i wyszukiwanie
        Opowiadań, ale nie filtr statusu promocji tekstu; kolumny jak w wymaganiach."""
        self.client.force_login(self.social)
        TextSocialMedia.objects.create(text=self.foreign, status='posted')
        page = self.client.get(URL)
        self.assertIsNone(self.quotes_section(page).get('open'))
        self.assertEqual(self.quotes_section(page).xpath('.//thead//th/text()'),
                         ['Antologia', 'Tytuł', 'Autor', 'Cytat', 'Kto dodał', 'Data wrzucenia', 'Status', 'Akcje'])
        cases = (
            ({}, ['Obcy', 'Smocze jajo']), ({'unpublished': '1'}, ['Obcy', 'Smocze jajo', 'Szkic']),
            ({'q': 'Le Guin'}, ['Obcy']), ({'anthology': self.ready.pk}, ['Smocze jajo']),
            ({'status': 'free'}, ['Obcy', 'Smocze jajo']),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                response = self.client.get(URL, {'quotes_page': '1', 'quotes_sort': 'title', **params})
                self.assertIsNotNone(self.quotes_section(response).get('open'))
                self.assertEqual(quote_titles(response), expected)

    def test_social_media_adds_quotes_and_many_people_quote_one_text(self):
        """Osoba od Social Media zapisuje cytat (data – dzisiaj, status „Wolne”, autor); powrót do listy z rozwiniętą
        sekcją (tylko raz) i kotwicą tekstu. Jeden tekst ma cytaty wielu osób w kolejności dodania; po usunięciu konta
        zostaje podpis."""
        for user, content in ((self.other, '  Smok spał\r\nna złocie.  '), (self.social, 'Drugi cytat')):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.add_quote(self.story, content, return_query='q=smo&quotes_page=1&evil=1')
                location = urlsplit(response['Location'])
                self.assertEqual((location.query, location.fragment),
                                 ('q=smo&quotes_page=1', f'social-quote-text-{self.story.pk}'))
                # Flaga sesji rozwija sekcję także bez parametrów cytatów – tylko raz.
                self.assertIsNotNone(self.quotes_section(self.client.get(URL)).get('open'))
                self.assertIsNone(self.quotes_section(self.client.get(URL)).get('open'))
        first, second = SocialMediaQuote.objects.order_by('pk')
        self.assertEqual((first.text, first.content, first.status, first.posted_on, first.added_by, first.added_by_name,
                          first.updated_by),
                         (self.story, 'Smok spał\nna złocie.', 'free', timezone.localdate(), self.other,
                          'social-other Testowy', self.other))
        self.assertEqual((second.added_by, second.content), (self.social, 'Drugi cytat'))
        self.other.delete()
        group = page_doc(self.client.get(URL)).xpath(f'//tbody[@id="social-quote-text-{self.story.pk}"]')[0]
        rows = [{td.get('data-label'): ' '.join(td.text_content().split()) for td in row.xpath('./td[@data-label]')}
                for row in group.xpath('./tr[@class="social-quote-row"]')]
        self.assertEqual([(row['Cytat'], row['Kto dodał']) for row in rows],
                         [('Smok spał na złocie.', 'social-other Testowy'), ('Drugi cytat', 'social-person Testowy')])
        self.assertEqual(SocialMediaQuote.objects.get(pk=first.pk).added_by, None)

    def test_social_media_changes_status_and_date_of_any_quote(self):
        """Osoba od Social Media zmienia status i datę cytatu dodanego przez kogoś innego (wiersz ma listę statusów
        i pole daty); powrót do tekstu cytatu."""
        quote = SocialMediaQuote.objects.create(text=self.story, content='Cytat', added_by=self.other)
        self.client.force_login(self.social)
        row = page_doc(self.client.get(URL)).xpath(f'//tr[@id="social-quote-{quote.pk}"]')[0]
        self.assertEqual((row.xpath('.//select[@name="status"]/option[@selected]/text()'),
                          row.xpath('.//input[@name="posted_on"]/@value')), (['Wolne'], [timezone.localdate().isoformat()]))
        response = self.change_quote(self.social, quote, 'quote_edit', status='posted', posted_on='2026-11-05',
                                     return_query='quotes_page=2')
        self.assertEqual(urlsplit(response['Location']).fragment, f'social-quote-text-{self.story.pk}')
        quote.refresh_from_db()
        self.assertEqual((quote.status, quote.posted_on, quote.updated_by),
                         ('posted', datetime.date(2026, 11, 5), self.social))

    def test_delete_any_quote_regardless_of_author_and_status(self):
        """Osoba od Social Media i koordynator usuwają dowolny cytat – własny i cudzy, w każdym statusie; przycisk
        „Usuń” z pytaniem jest przy każdym cytacie."""
        cases = (
            ('własny wolny', self.social, self.social, 'free'),
            ('cudzy wrzucony', self.social, self.other, 'posted'),
            ('cudzy w przygotowaniu – koordynator', self.coordinator, self.other, 'in_progress'),
        )
        for label, user, author, status in cases:
            with self.subTest(label):
                quote = SocialMediaQuote.objects.create(text=self.story, content=label, added_by=author, status=status)
                self.client.force_login(user)
                buttons = page_doc(self.client.get(URL)).xpath(
                    f'//form[@id="social-quote-form-{quote.pk}"]/button[@value="quote_delete"]/@data-confirm')
                self.assertEqual(buttons, ['Usunąć cytat do „Smocze jajo”?'])
                self.assertEqual(self.change_quote(user, quote, 'quote_delete').status_code, 302)
                self.assertFalse(SocialMediaQuote.objects.filter(pk=quote.pk).exists())

    def test_validation_keeps_section_open_and_rejects_texts_outside_list(self):
        """Pusty lub za długi cytat – 400 z błędem przy tekście, wpisaną treścią i rozwiniętą sekcją; tekst spoza listy
        (wycofany, nieistniejący) – 404, brak tekstu – 400; nic nie zostaje zapisane."""
        self.client.force_login(self.social)
        cases = (
            ('pusty', self.story.pk, '   ', 400, 'Wpisz cytat.'),
            ('za długi', self.story.pk, 'x' * 2001, 400, 'Cytat może mieć najwyżej 2000 znaków.'),
            ('wycofany tekst', Text.objects.get(title='Wycofany').pk, 'Cytat', 404, None),
            ('nieistniejący tekst', 999999, 'Cytat', 404, None),
            ('bez tekstu', '', 'Cytat', 400, None),
        )
        for label, text_pk, content, status, error in cases:
            with self.subTest(label):
                response = self.client.post(URL, {'action': 'quote_add', 'text': text_pk, 'content': content})
                self.assertEqual(response.status_code, status)
                if error:
                    section = self.quotes_section(response)
                    self.assertIsNotNone(section.get('open'))
                    group = section.xpath(f'.//tbody[@id="social-quote-text-{text_pk}"]')[0]
                    self.assertEqual(group.xpath('.//span[@class="field-error"]/text()'), [error])
                    self.assertEqual(group.xpath('.//textarea[@name="content"]/text()'), [content])
        self.assertFalse(SocialMediaQuote.objects.exists())

    def test_stale_quote_form_is_rejected(self):
        """Zmiana statusu z nieaktualnym formularzem – 409 z aktualnymi danymi, bez nadpisania; ponowny zapis z nowym
        tokenem przechodzi. Usunięcie z nieaktualnym formularzem nie usuwa cytatu."""
        quote = SocialMediaQuote.objects.create(text=self.story, content='Cytat', added_by=self.other)
        self.client.force_login(self.social)
        stale = self.row_version(self.client.get(URL), f'social-quote-form-{quote.pk}')
        SocialMediaQuote.objects.filter(pk=quote.pk).update(status='posted', posted_on=datetime.date(2026, 10, 1))
        response = self.client.post(URL, {'action': 'quote_edit', 'quote': quote.pk, 'version': stale,
                                          'status': 'in_progress', 'posted_on': '2026-10-02'})
        self.assertContains(response, 'Ktoś zmienił ten wpis w międzyczasie (obecnie: Wrzucone, 01.10.2026)',
                            status_code=409)
        self.assertIsNotNone(self.quotes_section(response).get('open'))
        quote.refresh_from_db()
        self.assertEqual(quote.status, 'posted')
        retry = self.row_version(response, f'social-quote-form-{quote.pk}')
        self.assertEqual(self.client.post(URL, {'action': 'quote_delete', 'quote': quote.pk, 'version': stale}).status_code,
                         302)
        self.assertTrue(SocialMediaQuote.objects.filter(pk=quote.pk).exists())
        self.assertEqual(self.client.post(URL, {'action': 'quote_edit', 'quote': quote.pk, 'version': retry,
                                                'status': 'in_progress', 'posted_on': ''}).status_code, 400)
        response = self.client.post(URL, {'action': 'quote_edit', 'quote': quote.pk, 'version': retry,
                                          'status': 'in_progress', 'posted_on': '2026-10-02'})
        self.assertEqual(response.status_code, 302)
        quote.refresh_from_db()
        self.assertEqual((quote.status, quote.posted_on), ('in_progress', datetime.date(2026, 10, 2)))

    def test_admin_for_superuser_only_records_who_added(self):
        """Cytaty w panelu admina widzi tylko superuser; dodanie cytatu w adminie zapisuje dodającego i jego podpis."""
        quote = SocialMediaQuote.objects.create(text=self.story, content='Cytat w adminie')
        changelist = reverse('admin:core_socialmediaquote_changelist')
        for label, user, allowed in (('superuser', self.admin, True), ('koordynator', self.coordinator, False),
                                     ('social media', self.social, False)):
            with self.subTest(user=label):
                self.client.force_login(user)
                response = self.client.get(changelist)
                self.assertEqual(response.status_code == 200, allowed)
                if allowed:
                    self.assertContains(response, str(quote))
        self.client.force_login(self.admin)
        response = self.client.post(reverse('admin:core_socialmediaquote_add'), {
            'text': self.foreign.pk, 'content': 'Z panelu', 'status': 'free', 'posted_on': '2026-10-03'})
        self.assertEqual(response.status_code, 302)
        added = SocialMediaQuote.objects.get(content='Z panelu')
        self.assertEqual((added.added_by, added.added_by_name, added.updated_by),
                         (self.admin, 'social-admin', self.admin))


class SocialMediaRoleMigrationTests(TestCase):
    def test_data_migration_creates_role_once(self):
        """Migracja people 0018 tworzy rolę „Social Media” (jest po migrate) i nie dubluje jej przy innej wielkości liter."""
        self.assertEqual(Role.objects.filter(name=roles.SOCIAL_MEDIA).count(), 1)
        migration = importlib.import_module('people.migrations.0018_add_social_media_role')
        editor = SimpleNamespace(connection=connection)
        Role.objects.filter(name=roles.SOCIAL_MEDIA).delete()
        migration.add_social_media_role(django_apps, editor)
        self.assertEqual(list(Role.objects.filter(name__iexact=roles.SOCIAL_MEDIA).values_list('name', flat=True)),
                         [roles.SOCIAL_MEDIA])
        Role.objects.filter(name=roles.SOCIAL_MEDIA).update(name='social media')
        migration.add_social_media_role(django_apps, editor)
        self.assertEqual(Role.objects.filter(name__iexact=roles.SOCIAL_MEDIA).count(), 1)
