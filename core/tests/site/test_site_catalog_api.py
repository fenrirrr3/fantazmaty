"""Endpoint ``/api/strona/katalog/``: klucz, JSON dokładnie w formacie bloku ``#fzs-data`` szukajki (tylko włączone
antologie, kolejność, listy bez pustych i powtórzeń), ``liczby`` jak liczniki na stronie, ślad pobrania."""
from django.test import TestCase, override_settings
from django.urls import reverse

from core.site_catalog import catalog_counts
from core.site_models import SiteCatalogAnthology, SiteCatalogStory, SiteTeamSettings

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
        """Bez SITE_API_KEY – 503; bez nagłówka, ze złym kluczem albo jego prefiksem – 403; POST – 405."""
        with override_settings(SITE_API_KEY=''):
            response = self.client.get(URL, HTTP_X_SITE_KEY='cokolwiek')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {'blad': 'Brak klucza SITE_API_KEY w konfiguracji.'})
        with override_settings(SITE_API_KEY='tajny-klucz'):
            for case, headers in (('brak nagłówka', {}), ('zły klucz', {'HTTP_X_SITE_KEY': 'inny'}),
                                  ('prefiks klucza', {'HTTP_X_SITE_KEY': 'tajny'})):
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
