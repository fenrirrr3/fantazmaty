"""Zakładka Social Media: dostęp, zakres opowiadań (antologie gotowe / niewydane), kolumny, zapis wierszy
z tokenem wersji (bez nadpisywania cudzych zmian), sekcja „Inne” i migracja danych roli."""
import datetime
import importlib
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.apps import apps as django_apps
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.models import SocialMediaEntry, TextSocialMedia
from fantazmaty.testing import create_member, create_superuser
from people import roles
from people.models import Role
from texts.models import Anthology, ForeignAuthor, Text, TextTranslation
from workflow.models import WorkflowStage

URL = reverse('core:social_media')


def page_doc(response):
    return html.fromstring(response.content)


def table_titles(response):
    return page_doc(response).xpath('//table[contains(@class, "social-media-table")][1]//tbody/tr/td[2]//strong/text()')


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
    def test_access_for_superuser_coordinators_and_social_media_role_only(self):
        """Stronę i zapis (POST) mają superuser, koordynator dowolnego działu i rola „Social Media”;
        redaktor i recenzent dostają 403, anonim – przekierowanie do logowania."""
        cases = (
            ('superuser', self.admin, 200), ('koordynator', self.coordinator, 200), ('social media', self.social, 200),
            ('redaktor', create_member('social-editor', roles.EDITOR), 403),
            ('recenzent', create_member('social-reviewer', roles.REVIEWER), 403),
            ('anonim', None, 302),
        )
        for label, user, expected in cases:
            with self.subTest(user=label):
                self.client.logout()
                if user:
                    self.client.force_login(user)
                self.assertEqual(self.client.get(URL).status_code, expected)
                response = self.client.post(URL, {'action': 'entry_add', 'description': f'Wpis: {label}',
                                                  'status': 'free'})
                self.assertEqual(response.status_code, 302 if expected == 200 else expected)
                if expected == 302:
                    self.assertTrue(response['Location'].startswith(reverse('login')))
                self.assertEqual(SocialMediaEntry.objects.filter(description=f'Wpis: {label}').exists(), expected == 200)


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
        """Liczba zapytań strony jest taka sama dla 1 i 10 opowiadań (autorzy, wpisy i wersje pobierane zbiorczo)."""
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
        """„Inne”: sekcja nad tabelą opowiadań, domyślnie zwinięta; dodanie, edycja i usunięcie wpisu wracają z rozwiniętą sekcją
        (tylko raz – następne wejście znów ją zwija); usuwanie ma pytanie potwierdzające."""
        page = self.client.get(URL)
        self.assertIsNone(self.other_section(page).get('open'))
        self.assertEqual([el.get('id') or el.get('aria-labelledby') for el in
                          page_doc(page).xpath('//details[@id="social-other"] | //section[contains(@class, "social-media-card")]')],
                         ['social-other', 'social-texts-heading'])
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
