"""Endpoint ``/api/strona/katalog/``: klucz, JSON dokładnie w formacie bloku ``#fzs-data`` szukajki (tylko włączone
antologie, kolejność, listy bez pustych i powtórzeń), ``liczby`` jak liczniki na stronie, ślad pobrania, dane na żywo
z CMS-u dla powiązanych wpisów (bez N+1) oraz ETag/304 wspólny dla endpointów ekipy, katalogu i „Słuchaj”."""
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from authors.models import Author
from core.site_catalog import catalog_counts, catalog_payload
from core.site_models import SiteCatalogAnthology, SiteCatalogStory, SiteTeamSettings
from texts.models import Anthology, ForeignAuthor, NovelProfile, Text, TextTranslation

from .test_site_listen_payload import ListenDataMixin

URL = reverse('core:site_catalog_api')


def catalog_anthology(title, position, **fields):
    fields.setdefault('url', f'https://fantazmaty.pl/czytaj/antologie/{position}/')
    return SiteCatalogAnthology.objects.create(title=title, position=position, **fields)


def story(owner, title, author='Autor Testowy', genres='', tags='', position=None, **fields):
    position = position or owner.stories.count() + 1
    return SiteCatalogStory.objects.create(anthology=owner, title=title, author=author, genres=genres, tags=tags,
                                           position=position, **fields)


class SiteCatalogApiAccessTests(TestCase):
    def test_missing_key_gives_503_wrong_header_403_post_405(self):
        """Bez SITE_API_KEY – 503; bez nagłówka, ze złym kluczem, jego prefiksem albo kluczem ze znakami spoza
        ASCII – 403 (nie 500); POST – 405."""
        with override_settings(SITE_API_KEY=''):
            response = self.client.get(URL, HTTP_X_SITE_KEY='cokolwiek')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {'blad': 'Brak klucza SITE_API_KEY w konfiguracji.'})
        with override_settings(SITE_API_KEY='tajny-klucz'):
            for case, headers in (('brak nagłówka', {}), ('zły klucz', {'HTTP_X_SITE_KEY': 'inny'}),
                                  ('prefiks klucza', {'HTTP_X_SITE_KEY': 'tajny'}),
                                  ('znaki spoza ASCII', {'HTTP_X_SITE_KEY': 'tajny-klucz-żółć'})):
                with self.subTest(case):
                    response = self.client.get(URL, **headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json(), {'blad': 'Brak dostępu.'})
            self.assertEqual(self.client.post(URL, HTTP_X_SITE_KEY='tajny-klucz').status_code, 405)
            self.assertEqual(self.client.get(URL, HTTP_X_SITE_KEY='tajny-klucz').status_code, 200)


@override_settings(SITE_API_KEY='tajny-klucz')
class SiteCatalogApiPayloadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        SiteCatalogAnthology.objects.all().delete()
        cls.second = catalog_anthology('Druga', 2)
        story(cls.second, 'Opowiadanie B', 'Anna Nowak', genres='Fantasy\n\n fantasy \nhorror', tags='smoki\nSmoki\nmagia')
        cls.first = catalog_anthology('Pierwsza', 1)
        story(cls.first, 'Opowiadanie A', 'Jan Kowalski, Ola Zięba', genres='science fiction', tags='kosmos\nmagia')
        story(cls.first, 'Bez gatunku', 'Jan Kowalski', genres='', tags='')
        cls.off = catalog_anthology('Wyłączona', 3, is_enabled=False)
        story(cls.off, 'Ukryte', 'Tajny Autor', genres='ukryty gatunek', tags='ukryty tag')

    def fetch(self):
        response = self.client.get(URL, HTTP_X_SITE_KEY='tajny-klucz')
        self.assertEqual(response.status_code, 200)
        return response

    def test_payload_matches_site_format(self):
        """Tylko włączone antologie w kolejności ``position``; opowiadanie ma dokładnie klucze tytul/autor/gatunki/tagi;
        listy bez pustych wierszy i powtórzeń (bez wielkości liter), w kolejności zapisu; ``liczby`` liczą unikalne
        gatunki (opowiadanie bez gatunku to osobny gatunek „do ustalenia”) i tagi."""
        data = self.fetch().json()
        self.assertEqual(set(data), {'wersja', 'liczby', 'antologie'})
        self.assertEqual(data['antologie'], [
            {'nazwa': 'Pierwsza', 'url': 'https://fantazmaty.pl/czytaj/antologie/1/', 'opowiadania': [
                {'tytul': 'Opowiadanie A', 'autor': 'Jan Kowalski, Ola Zięba', 'gatunki': ['science fiction'],
                 'tagi': ['kosmos', 'magia']},
                {'tytul': 'Bez gatunku', 'autor': 'Jan Kowalski', 'gatunki': [], 'tagi': []}]},
            {'nazwa': 'Druga', 'url': 'https://fantazmaty.pl/czytaj/antologie/2/', 'opowiadania': [
                {'tytul': 'Opowiadanie B', 'autor': 'Anna Nowak', 'gatunki': ['Fantasy', 'horror'],
                 'tagi': ['smoki', 'magia']}]},
        ])
        self.assertEqual(data['liczby'], {'antologie': 2, 'opowiadania': 3, 'gatunki': 4, 'tagi': 3})

    def test_counts_fold_diacritics_like_the_site(self):
        """Gatunki i tagi różniące się tylko wielkością liter albo diakrytykami liczą się raz (jak w JS strony)."""
        anthologies = [{'opowiadania': [
            {'gatunki': ['Groza', 'groza', 'GROZA'], 'tagi': ['Łódź', 'lodz', 'łódź']},
            {'gatunki': ['science fiction'], 'tagi': []}]}]
        self.assertEqual(catalog_counts(anthologies), {'antologie': 1, 'opowiadania': 2, 'gatunki': 2, 'tagi': 1})
        self.assertEqual(catalog_counts([]), {'antologie': 0, 'opowiadania': 0, 'gatunki': 0, 'tagi': 0})

    def test_response_is_not_cached_has_no_hidden_data_and_records_fetch(self):
        """Nagłówek no-store, JSON bez ASCII-escape, bez wyłączonej antologii i bez ``@``; ślad pobrania katalogu
        zapisany osobno od śladu ekipy."""
        self.assertIsNone(SiteTeamSettings.load().catalog_fetched_at)
        response = self.fetch()
        self.assertIn('no-store', response['Cache-Control'])
        content = response.content.decode()
        self.assertIn('Zięba', content)
        for forbidden in ('@', 'Wyłączona', 'Ukryte', 'Tajny Autor', 'ukryty tag'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, content)
        settings = SiteTeamSettings.load()
        self.assertIsNotNone(settings.catalog_fetched_at)
        self.assertEqual(settings.catalog_fetch_version, response.json()['wersja'])
        self.assertIsNone(settings.last_fetched_at)


def cms_text(anthology, title, *authors, **fields):
    created = Text.objects.create(title=title, anthology=anthology, length=1000, **fields)
    created.authors.add(*authors)
    return created


class SiteCatalogLiveDataTests(TestCase):
    """Wpisy powiązane z CMS-em: nazwa, tytuł i autor z CMS-u, gatunki i tagi łączone; wpisy spoza CMS-u bez zmian."""

    @classmethod
    def setUpTestData(cls):
        SiteCatalogAnthology.objects.all().delete()
        cls.jan = Author.objects.create(first_name='Jan', last_name='Kowalski')
        cls.anna = Author.objects.create(first_name='Anna', last_name='Nowak')
        # Antologia niegotowa: dane na żywo dotyczą każdego powiązanego wpisu, nie tylko importowalnych.
        cls.book = Anthology.objects.create(title='Smocza antologia')
        cls.text = cms_text(cls.book, 'Smocze jajo', cls.jan, cls.anna, genre='Fantasy, groza', tags='smoki, Łódź')
        cls.linked = catalog_anthology('Stara nazwa', 1, anthology=cls.book)
        cls.story = story(cls.linked, 'Stary tytuł', 'Stary Autor', genres='fantasy\nbaśń', tags='lodz\nręczny',
                          text=cls.text)
        cls.orphan = catalog_anthology('Spoza CMS-u', 2)
        story(cls.orphan, 'Ręczne', 'Obcy Autor', genres='horror', tags='mgła')

    def entries(self):
        return {item['nazwa']: item['opowiadania'] for item in catalog_payload()['antologie']}

    def test_linked_story_and_anthology_follow_cms_at_once(self):
        """Bez żadnego przycisku: zmiana tytułu tekstu, inicjał dopisany autorowi i nowa nazwa antologii są od razu
        w JSON-ie (autorzy jako „Imię Nazwisko, Imię Nazwisko”); gatunki i tagi – najpierw z katalogu, potem nowe
        z CMS-u (bez powtórzeń wg wielkości liter i diakrytyków); wpis spoza CMS-u – jak w katalogu."""
        self.assertEqual(self.entries(), {
            'Smocza antologia': [{'tytul': 'Smocze jajo', 'autor': 'Jan Kowalski, Anna Nowak',
                                  'gatunki': ['fantasy', 'baśń', 'groza'], 'tagi': ['lodz', 'ręczny', 'smoki']}],
            'Spoza CMS-u': [{'tytul': 'Ręczne', 'autor': 'Obcy Autor', 'gatunki': ['horror'], 'tagi': ['mgła']}],
        })
        Text.objects.filter(pk=self.text.pk).update(title='Smocze jajo (wersja autorska)', tags='smoki, nowy tag')
        Author.objects.filter(pk=self.jan.pk).update(first_name='Jan M.')
        Anthology.objects.filter(pk=self.book.pk).update(title='Smocza antologia II')
        SiteCatalogAnthology.objects.filter(pk=self.orphan.pk).update(title='Spoza CMS-u (2024)')
        self.assertEqual(self.entries(), {
            'Smocza antologia II': [{'tytul': 'Smocze jajo (wersja autorska)', 'autor': 'Jan M. Kowalski, Anna Nowak',
                                     'gatunki': ['fantasy', 'baśń', 'groza'],
                                     'tagi': ['lodz', 'ręczny', 'smoki', 'nowy tag']}],
            'Spoza CMS-u (2024)': [{'tytul': 'Ręczne', 'autor': 'Obcy Autor', 'gatunki': ['horror'], 'tagi': ['mgła']}],
        })
        self.story.refresh_from_db()
        self.assertEqual((self.story.title, self.story.author), ('Stary tytuł', 'Stary Autor'))

    def test_fallbacks_when_cms_has_no_value_or_link_is_lost(self):
        """Tekst bez autorów – autor z katalogu; zerwane powiązanie (usunięty tekst albo antologia) – dane z katalogu."""
        self.text.authors.clear()
        self.assertEqual(self.entries()['Smocza antologia'][0]['autor'], 'Stary Autor')
        SiteCatalogStory.objects.filter(pk=self.story.pk).update(text=None)
        SiteCatalogAnthology.objects.filter(pk=self.linked.pk).update(anthology=None)
        self.assertEqual(self.entries()['Stara nazwa'], [{'tytul': 'Stary tytuł', 'autor': 'Stary Autor',
                                                          'gatunki': ['fantasy', 'baśń'], 'tagi': ['lodz', 'ręczny']}])

    def test_payload_query_count_does_not_grow_with_entries(self):
        """Budowa JSON-u bez N+1: liczba zapytań jest stała, gdy przybywa powiązanych opowiadań z autorami, antologii
        tłumaczeń z autorami zagranicznymi i powieści z profilem."""
        def count():
            with CaptureQueriesContext(connection) as queries:
                catalog_payload()
            return len(queries)

        def add_entries(suffix):
            for number in range(3):
                story(self.linked, f'Wpis {suffix}{number}',
                      text=cms_text(self.book, f'Tekst {suffix}{number}', self.jan, self.anna))
            translated = Anthology.objects.create(title=f'Przekłady {suffix}', is_translated=True)
            foreign = cms_text(translated, 'Obcy')
            TextTranslation.objects.get_or_create(text=foreign)[0].foreign_authors.add(
                ForeignAuthor.objects.create(first_name='Ursula', last_name=f'Le Guin {suffix}'))
            story(catalog_anthology(translated.title, 3, anthology=translated), 'Obcy', text=foreign)
            novel = Anthology.objects.create(title=f'Powieść {suffix}', is_novel=True)
            NovelProfile.objects.get_or_create(anthology=novel)[0].authors.add(self.anna)
            story(catalog_anthology(novel.title, 4, anthology=novel), 'Powieść')

        add_entries('a')
        before = count()
        add_entries('b')
        self.assertEqual(count(), before)
        entries = self.entries()
        self.assertEqual((entries['Przekłady b'][0]['autor'], entries['Powieść b'][0]['autor']),
                         ('Ursula Le Guin b', 'Anna Nowak'))


@override_settings(SITE_API_KEY='tajny-klucz')
class SiteApiConditionalGetTests(ListenDataMixin, TestCase):
    """ETag i 304 – wspólne dla ``/api/strona/ekipa/``, ``/katalog/`` i ``/sluchaj/``."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.catalog = catalog_anthology('Antologia ETag', 99)
        story(cls.catalog, 'Opowiadanie ETag')

    def endpoints(self):
        """(nazwa, adres, pola śladu pobrania, zmiana danych w CMS-ie)."""
        return (
            ('ekipa', reverse('core:site_team_api'), ('last_fetched_at', 'last_fetch_version'),
             lambda: SiteTeamSettings.objects.filter(pk=1).update(lead_editor='Anna Nowa')),
            ('katalog', URL, ('catalog_fetched_at', 'catalog_fetch_version'),
             lambda: SiteCatalogAnthology.objects.filter(pk=self.catalog.pk).update(title='Antologia ETag 2')),
            ('słuchaj', reverse('core:site_listen_api'), ('listen_fetched_at', 'listen_fetch_version'),
             lambda: Text.objects.filter(pk=self.full.text_id).update(title='Pełne – nowy tytuł')),
        )

    def test_etag_304_and_fetch_record(self):
        """200 z ETag równym ``wersja`` (skrót treści – dwa pobrania bez zmian dają to samo); ``If-None-Match``
        z tą wartością – 304 bez treści z tym samym ETag i zapisanym śladem pobrania; po zmianie danych stary ETag
        daje 200 z nowym; bez klucza – 403 także z ``If-None-Match``."""
        SiteTeamSettings.load()
        for name, url, (fetched_at, fetch_version), change in self.endpoints():
            with self.subTest(endpoint=name):
                first = self.client.get(url, HTTP_X_SITE_KEY='tajny-klucz')
                etag = first['ETag']
                self.assertEqual((first.status_code, etag), (200, f'"{first.json()["wersja"]}"'))
                self.assertEqual(self.client.get(url, HTTP_X_SITE_KEY='tajny-klucz')['ETag'], etag)
                SiteTeamSettings.objects.filter(pk=1).update(**{fetched_at: None, fetch_version: ''})
                cached = self.client.get(url, HTTP_X_SITE_KEY='tajny-klucz', HTTP_IF_NONE_MATCH=etag)
                self.assertEqual((cached.status_code, cached.content, cached['ETag']), (304, b'', etag))
                settings = SiteTeamSettings.load()
                self.assertIsNotNone(getattr(settings, fetched_at))
                self.assertEqual(f'"{getattr(settings, fetch_version)}"', etag)
                change()
                fresh = self.client.get(url, HTTP_X_SITE_KEY='tajny-klucz', HTTP_IF_NONE_MATCH=etag)
                self.assertEqual(fresh.status_code, 200)
                self.assertNotEqual(fresh['ETag'], etag)
                denied = self.client.get(url, HTTP_IF_NONE_MATCH=fresh['ETag'])
                self.assertEqual((denied.status_code, denied.json()), (403, {'blad': 'Brak dostępu.'}))
