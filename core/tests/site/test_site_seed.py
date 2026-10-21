"""Seedy z migracji core 0060 (działy strony „Ekipa”) i 0063 (katalog szukajki z ``core/site_catalog_seed.json``)
są w świeżej bazie; dopasowanie katalogu do antologii i tekstów CMS-u."""
import json
from pathlib import Path

from django.test import TestCase

from core.site_catalog import anthology_key, catalog_payload, fold, link_catalog_to_cms
from core.site_models import SiteCatalogAnthology, SiteCatalogStory, SiteTeamGroup, SiteTeamSection, SiteTeamSettings
from people import roles
from texts.models import Anthology, Text

SEED = Path(__file__).resolve().parents[2] / 'site_catalog_seed.json'


class SiteTeamSeedTests(TestCase):
    def test_sections_order_roles_and_manual_audio_names(self):
        """Osiem działów w kolejności strony, role grup i koordynacji, notka Ilustracji, ręczne nazwiska audio,
        etykieta „Audio” w nawigacji i rekord ustawień z domyślnymi osobami."""
        sections = {item.slug: item for item in SiteTeamSection.objects.prefetch_related('groups__roles', 'coordinator_roles')}
        self.assertEqual(list(sections), ['redakcja', 'korekta', 'korekta-poskladowa', 'weryfikacja', 'recenzje',
                                          'ilustracje', 'grafika-sklad', 'audio'])
        expected = {
            'redakcja': ('Redakcja', True, [roles.EDITING_COORDINATOR], [('', 'roles', [roles.EDITOR])]),
            'korekta': ('Korekta', True, [roles.PROOFREADING_COORDINATOR], [('', 'roles', [roles.PROOFREADER])]),
            'korekta-poskladowa': ('Korekta poskładowa', True, [], [('', 'roles', [roles.POST_LAYOUT_PROOFREADER])]),
            'weryfikacja': ('Weryfikacja', True, [roles.VERIFICATION_COORDINATOR], [('', 'roles', [roles.VERIFIER])]),
            'recenzje': ('Recenzje', True, [roles.REVIEW_COORDINATOR], [('', 'roles', [roles.REVIEWER])]),
            'ilustracje': ('Ilustracje', False, [roles.ILLUSTRATION_COORDINATOR], []),
            'grafika-sklad': ('Grafika i skład', False, [],
                              [('Grafika', 'roles', [roles.DESIGNER]), ('Skład', 'roles', [roles.TYPESETTER])]),
            'audio': ('Dział audio', False, [roles.AUDIOBOOK_COORDINATOR], [
                ('Lektorzy', 'manual', ['Aleksandra Czyżewska', 'Izabella Dobrowolska', 'Sebastian Filipowicz',
                                        'Rafał Flejter', 'Jakub Kloch', 'Paweł Roicki', 'M. Stella Sit',
                                        'Karolina Stachacz-Bartosik', 'Ela Zińczuk']),
                ('Dźwiękowcy', 'manual', ['Teodor Laskowski']),
                ('Korektorzy', 'manual', ['Jadwiga Glaza', 'Anna Koral', 'Agnieszka Nowak', 'Agnieszka Rzepecka',
                                          'Aneta Stawiszyńska-Marciniak', 'Katarzyna Twarduś'])]),
        }
        for slug, (title, show_count, coordinators, groups) in expected.items():
            with self.subTest(slug=slug):
                item = sections[slug]
                self.assertEqual((item.title, item.show_count, item.is_enabled, item.in_navigation),
                                 (title, show_count, True, True))
                self.assertEqual([role.name for role in item.coordinator_roles.all()], coordinators)
                self.assertEqual([(group.title, group.source,
                                   group.manual_list if group.source == SiteTeamGroup.Source.MANUAL
                                   else [role.name for role in group.roles.all()])
                                  for group in item.groups.all()], groups)
        self.assertIn('<a href="https://fantazmaty.pl/czytaj/antologie/">antologii</a>', sections['ilustracje'].note)
        self.assertEqual(sections['audio'].navigation_label, 'Audio')
        settings = SiteTeamSettings.objects.get(pk=1)
        self.assertEqual((settings.lead_editor, settings.site_person), ('Dawid Wiktorski', 'Dawid Wiktorski'))


class SiteCatalogSeedTests(TestCase):
    def test_catalog_matches_seed_file(self):
        """27 antologii i 297 opowiadań w kolejności pliku, z adresami https://fantazmaty.pl/…, gatunkami i tagami
        po jednym w wierszu; w pustej bazie bez powiązań z CMS-em."""
        data = json.loads(SEED.read_text(encoding='utf-8'))
        anthologies = list(SiteCatalogAnthology.objects.prefetch_related('stories'))
        self.assertEqual(len(anthologies), 27)
        self.assertEqual(SiteCatalogStory.objects.count(), 297)
        self.assertEqual([item.title for item in anthologies], [item['nazwa'] for item in data['antologie']])
        self.assertEqual([item.position for item in anthologies], list(range(1, 28)))
        for item, expected in zip(anthologies, data['antologie'], strict=True):
            with self.subTest(anthology=item.title):
                self.assertTrue(item.url.startswith('https://fantazmaty.pl/'))
                self.assertTrue(item.is_enabled)
                self.assertIsNone(item.anthology_id)
                self.assertEqual([(story.title, story.author, story.genre_list, story.tag_list)
                                  for story in item.stories.all()],
                                 [(story['tytul'], story['autor'], story['gatunki'], story['tagi'])
                                  for story in expected['opowiadania']])
        self.assertFalse(SiteCatalogStory.objects.exclude(text__isnull=True).exists())
        self.assertEqual(SiteTeamSettings.load().catalog_page_url, '')

    def test_seed_payload_counts_match_live_search_page(self):
        """JSON dla strony z seedu ma te same liczby co szukajka na fantazmaty.pl (84 gatunki i 819 tagów
        w filtrach – liczone bez wielkości liter i diakrytyków) i każde opowiadanie z tytułem, bez którego
        mechanizm wyszukiwarki odrzuca cały katalog."""
        data = catalog_payload()
        self.assertEqual(data['liczby'], {'antologie': 27, 'opowiadania': 297, 'gatunki': 84, 'tagi': 819})
        self.assertTrue(all(item['nazwa'].strip() and all(story['tytul'].strip() for story in item['opowiadania'])
                            for item in data['antologie']))

    def test_link_catalog_to_cms_matches_titles(self):
        """Ponowne dopasowanie: antologia po tytule (rzymskie I–IV jak 1–4, bez wielkości liter), teksty po tytule
        w antologii (ujednolicone apostrofy); niejednoznaczne i nieznane tytuły zostają bez powiązania;
        dopasowanie nie nadpisuje istniejących powiązań."""
        book = Anthology.objects.create(title='Kocha, Lubi, Zgubi', status=Anthology.Status.READY)
        matched = Text.objects.create(title='Wzór na długie i szczęśliwe życie Jeremy’ego Barretta', anthology=book,
                                      length=100)
        roman = Anthology.objects.create(title='Fantazmaty I')
        Text.objects.create(title='Nieznany tekst', anthology=book, length=100)
        for title in ('Dwa razy', 'dwa RAZY'):
            Text.objects.create(title=title, anthology=book, length=100)
        for title in ('dragoneza', 'DRAGONEZA'):
            Anthology.objects.create(title=title)
        self.assertEqual(link_catalog_to_cms(), {'antologie': 2, 'opowiadania': 1})
        catalog = SiteCatalogAnthology.objects.get(title='Kocha, lubi, zgubi')
        self.assertEqual(catalog.anthology, book)
        self.assertEqual(SiteCatalogAnthology.objects.get(title='Fantazmaty 1').anthology, roman)
        self.assertEqual(SiteCatalogStory.objects.get(text=matched).anthology, catalog)
        self.assertIsNone(SiteCatalogAnthology.objects.get(title='Dragoneza').anthology_id)
        self.assertEqual(link_catalog_to_cms(), {'antologie': 0, 'opowiadania': 0})

    def test_keys_fold_quotes_case_diacritics_and_roman_numerals(self):
        """Klucze porównania: cudzysłowy i apostrofy ujednolicone, bez wielkości liter i diakrytyków, pojedyncze
        odstępy; rzymskie I–IV w kluczu antologii odpowiadają cyfrom."""
        for value, expected in (('Granica Roche’a', "granica roche'a"), ('„Łódź”  Żółta', '"lodz" zolta'),
                                ('  A   B ', 'a b'), ('', '')):
            with self.subTest(value=value):
                self.assertEqual(fold(value), expected)
        for value, expected in (('Fantazmaty I', 'fantazmaty 1'), ('Fantazmaty IV', 'fantazmaty 4'),
                                ('Fantazmaty 4', 'fantazmaty 4'), ('Ja, legenda', 'ja, legenda')):
            with self.subTest(value=value):
                self.assertEqual(anthology_key(value), expected)
