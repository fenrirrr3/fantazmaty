"""Synchronizacja katalogu szukajki z CMS-em: plan bez zapisu i jego zastosowanie (nowe opowiadania, raport wpisów
wycofanych i niepowiązanych), tylko antologie Gotowe spoza Ekstraktów, powieść jako jedna pozycja z danych powieści,
dodanie antologii z CMS-u i ostrzeżenia zakładki (na danych na żywo)."""
from django.test import TestCase

from authors.models import Author
from core.site_catalog import (apply_add, apply_update, candidate_anthologies, catalog_warnings, cms_texts, plan_add,
                               plan_digest, plan_update, split_genres, story_entry)
from core.site_models import SiteCatalogAnthology
from texts.models import Anthology, ForeignAuthor, NovelProfile, Text, TextTranslation
from workflow.models import WorkflowStage

from .test_site_catalog_api import catalog_anthology, story


def text(anthology, title, *authors, **fields):
    fields.setdefault('length', 1000)
    created = Text.objects.create(title=title, anthology=anthology, **fields)
    created.authors.add(*(Author.objects.create(first_name=first, last_name=last, pseudonym=pseudonym)
                          for first, last, pseudonym in authors))
    return created


def withdraw(item):
    WorkflowStage.objects.create(text=item, stage_type='withdrawn', is_released=True, is_completed=False)


class CatalogSyncDataMixin:
    @classmethod
    def setUpTestData(cls):
        SiteCatalogAnthology.objects.all().delete()
        cls.book = Anthology.objects.create(title='Smocza antologia', status=Anthology.Status.READY)
        cls.known = text(cls.book, 'Smocze jajo', ('Jan', 'Kowalski', ''), ('Anna', 'Nowak', 'A. N. Pióro'),
                         genre='Fantasy, groza | weird fiction', tags='smoki, magia, Łódź')
        cls.fresh = text(cls.book, 'Nowe opowiadanie', ('Ola', 'Zięba', ''), genre='science fiction', tags='kosmos')
        cls.gone = text(cls.book, 'Wycofane', ('Ktoś', 'Obcy', ''), genre='horror', tags='x')
        withdraw(cls.gone)
        cls.catalog = catalog_anthology('Smocza antologia', 1, anthology=cls.book)
        cls.story = story(cls.catalog, 'Jajo smocze', 'Jan Kowalski, A. N. Pióro', genres='fantasy\nbaśń',
                          tags='Smoki\nręcznie dodany', text=cls.known)
        cls.manual = story(cls.catalog, 'Spoza CMS-u', 'Autor Zewnętrzny', genres='fantasy', tags='tag')


class PlanUpdateTests(CatalogSyncDataMixin, TestCase):
    def test_plan_lists_new_stories_and_unlinked_without_saving(self):
        """Plan: tylko nowe opowiadanie z CMS-u (bez wycofanego) i wpis bez powiązania; powiązany wpis z innym tytułem
        i brakującymi gatunkami nie jest zmianą (strona bierze je z CMS-u na żywo); nic nie zapisane."""
        plan = plan_update(self.catalog)
        self.assertEqual(set(plan), {'anthology', 'new_stories', 'unlinked', 'missing', 'novel', 'has_changes'})
        self.assertEqual(plan['new_stories'], [{'text_id': self.fresh.pk, 'tytul': 'Nowe opowiadanie',
                                                'autor': 'Ola Zięba', 'gatunki': ['science fiction'],
                                                'tagi': ['kosmos'], 'ostrzezenia': []}])
        self.assertEqual(plan['unlinked'], [self.manual])
        self.assertEqual(plan['missing'], [])
        self.assertTrue(plan['has_changes'])
        self.story.refresh_from_db()
        self.assertEqual(self.story.genre_list, ['fantasy', 'baśń'])
        self.assertEqual(self.catalog.stories.count(), 2)

    def test_apply_adds_without_removing_and_second_run_has_no_changes(self):
        """Zastosowanie dopisuje nowe opowiadanie na końcu; istniejące wpisy zostają bez zmian (ich gatunki i tagi
        z CMS-u dokłada dopiero JSON); drugi plan nie ma zmian; skrót planu zmienia się wraz z danymi."""
        plan = plan_update(self.catalog)
        digest = plan_digest(plan)
        self.assertEqual(apply_update(plan), 1)
        self.story.refresh_from_db()
        self.assertEqual((self.story.title, self.story.genre_list, self.story.tag_list),
                         ('Jajo smocze', ['fantasy', 'baśń'], ['Smoki', 'ręcznie dodany']))
        titles = list(self.catalog.stories.values_list('title', 'position', 'text_id'))
        self.assertEqual(titles, [('Jajo smocze', 1, self.known.pk), ('Spoza CMS-u', 2, None),
                                  ('Nowe opowiadanie', 3, self.fresh.pk)])
        again = plan_update(self.catalog)
        self.assertFalse(again['has_changes'])
        self.assertEqual(again['new_stories'], [])
        self.assertNotEqual(plan_digest(again), digest)
        self.assertEqual(apply_update(again), 0)

    def test_withdrawn_chapters_extracts_and_unready_are_skipped(self):
        """Źródło z CMS-u pomija wycofane teksty, rozdziały powieści, Ekstrakty i antologie niegotowe; powiązany
        tekst wycofany po dodaniu trafia do raportu; aktualizacja wpisu bez powiązania, z Ekstraktami albo
        z antologią niegotową (W przygotowaniu, Porzucona) kończy się ``ValueError``."""
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        Text.objects.create(chapter_number=1, anthology=novel)
        extracts = Anthology.objects.create(title='Ekstrakty', is_extracts=True, status=Anthology.Status.READY)
        unready = Anthology.objects.create(title='W przygotowaniu')
        abandoned = Anthology.objects.create(title='Porzucona', status=Anthology.Status.ABANDONED)
        for book in (novel, extracts, unready, abandoned, None):
            with self.subTest(book=book):
                self.assertEqual(list(cms_texts(book)), [])
        self.assertEqual(list(cms_texts(self.book)), [self.fresh, self.known])
        story(self.catalog, 'Wycofane', 'Ktoś Obcy', genres='horror', tags='x', text=self.gone)
        plan = plan_update(self.catalog)
        self.assertEqual([item.title for item in plan['missing']], ['Wycofane'])
        for position, (title, book, message) in enumerate((
                ('Spoza CMS-u', None, 'nie jest powiązany'), ('Ekstrakty', extracts, 'Ekstrakty'),
                ('Niegotowa', unready, 'Gotowa'), ('Porzucona', abandoned, 'Gotowa')), start=2):
            with self.subTest(title=title):
                entry = catalog_anthology(title, position, anthology=book)
                with self.assertRaisesRegex(ValueError, message):
                    plan_update(entry)

    def test_novel_is_one_entry_from_its_profile(self):
        """Gotowa powieść: jedna pozycja z tytułem antologii, autorami, gatunkami i tagami z danych powieści, bez
        powiązania z tekstem; rozdziały pomijane; zmiany profilu nie wymagają aktualizacji – pozycja na stronie bierze
        tytuł, autorów, gatunki i tagi z profilu na żywo; pusty profil daje pozycję z ostrzeżeniami."""
        novel = Anthology.objects.create(title='Smocza powieść', is_novel=True)
        profile = NovelProfile.objects.get_or_create(anthology=novel)[0]
        profile.genre, profile.tags = 'fantasy | epicka', 'smoki, wojna'
        profile.save()
        profile.authors.add(Author.objects.create(first_name='Jan', last_name='Kowalski'),
                            Author.objects.create(first_name='Anna', last_name='Nowak', pseudonym='A. N. Pióro'))
        for number in (1, 2):
            Text.objects.create(chapter_number=number, anthology=novel, genre='rozdziałowy', tags='rozdział')
        with self.assertRaisesRegex(ValueError, 'Gotowa'):
            plan_add(novel)
        Anthology.objects.filter(pk=novel.pk).update(status=Anthology.Status.READY)
        novel.refresh_from_db()
        self.assertIn(novel, candidate_anthologies())
        plan = plan_add(novel)
        self.assertEqual(plan['stories'], [{'text_id': None, 'tytul': 'Smocza powieść',
                                            'autor': 'Jan Kowalski, A. N. Pióro', 'gatunki': ['fantasy', 'epicka'],
                                            'tagi': ['smoki', 'wojna'], 'powiesc': True, 'ostrzezenia': []}])
        added = apply_add(novel, 'https://fantazmaty.pl/czytaj/antologie/smocza/', plan)
        self.assertEqual(list(added.stories.values_list('title', 'text_id', 'genres', 'tags')),
                         [('Smocza powieść', None, 'fantasy\nepicka', 'smoki\nwojna')])
        self.assertFalse(plan_update(added)['has_changes'])
        profile.tags = 'smoki, wojna, magia'
        profile.genre = 'Fantasy, epicka, przygodowa'
        profile.save()
        profile.authors.add(Author.objects.create(first_name='Ewa', last_name='Lis'))
        Anthology.objects.filter(pk=novel.pk).update(title='Smocza powieść. Tom 1')
        added = SiteCatalogAnthology.objects.select_related('anthology').get(pk=added.pk)
        plan = plan_update(added)
        self.assertEqual((plan['new_stories'], plan['unlinked'], plan['missing'], plan['novel']), ([], [], [], True))
        self.assertEqual(story_entry(added.stories.get()), {
            'tytul': 'Smocza powieść. Tom 1', 'autor': 'Jan Kowalski, Ewa Lis, A. N. Pióro',
            'gatunki': ['fantasy', 'epicka', 'przygodowa'], 'tagi': ['smoki', 'wojna', 'magia']})
        bare = Anthology.objects.create(title='Pusty profil', is_novel=True)
        Anthology.objects.filter(pk=bare.pk).update(status=Anthology.Status.READY)
        bare.refresh_from_db()
        self.assertEqual(plan_add(bare)['stories'][0]['ostrzezenia'], ['brak gatunku', 'brak tagów', 'brak autora'])

    def test_novel_entry_without_stories_gets_its_single_position(self):
        """Wpis katalogu powieści bez pozycji (np. usuniętej w panelu): plan proponuje jedną pozycję powieści,
        zastosowanie ją dodaje, a kolejny plan nie ma zmian (pozycja nie jest dodawana drugi raz)."""
        novel = Anthology.objects.create(title='Pusta powieść', is_novel=True)
        Anthology.objects.filter(pk=novel.pk).update(status=Anthology.Status.READY)
        profile = NovelProfile.objects.get_or_create(anthology=novel)[0]
        profile.genre, profile.tags = 'groza', 'mgła'
        profile.save()
        entry = SiteCatalogAnthology.objects.select_related('anthology').get(
            pk=catalog_anthology('Pusta powieść', 5, anthology=novel).pk)
        plan = plan_update(entry)
        self.assertEqual([(item['tytul'], item['text_id'], item['gatunki']) for item in plan['new_stories']],
                         [('Pusta powieść', None, ['groza'])])
        self.assertEqual(apply_update(plan), 1)
        self.assertFalse(plan_update(entry)['has_changes'])
        self.assertEqual(list(entry.stories.values_list('title', 'tags')), [('Pusta powieść', 'mgła')])

    def test_translated_anthology_uses_foreign_authors(self):
        """W antologii tłumaczeń autorem jest autor zagraniczny (pseudonim albo imię i nazwisko), nie tłumacz."""
        translated = Anthology.objects.create(title='Przekłady', status=Anthology.Status.READY, is_translated=True)
        foreign = Text.objects.create(title='Obcy', anthology=translated, length=1000, genre='SF')
        TextTranslation.objects.get_or_create(text=foreign)[0].foreign_authors.add(
            ForeignAuthor.objects.create(first_name='Ursula', last_name='Le Guin', pseudonym='U. K. Le Guin'))
        plan = plan_add(translated)
        self.assertEqual([(s['tytul'], s['autor'], s['ostrzezenia']) for s in plan['stories']],
                         [('Obcy', 'U. K. Le Guin', ['brak tagów'])])

    def test_split_genres_accepts_commas_semicolons_and_bars(self):
        """Gatunki z pola CMS-u rozdzielone przecinkiem, średnikiem albo kreską pionową, bez pustych i powtórzeń."""
        for value, expected in (('fantasy', ['fantasy']), ('a, b; c | d', ['a', 'b', 'c', 'd']),
                                ('a,, A ,a', ['a']), ('', [])):
            with self.subTest(value=value):
                self.assertEqual(split_genres(value), expected)


class PlanAddTests(CatalogSyncDataMixin, TestCase):
    def test_candidates_plan_and_apply(self):
        """Kandydaci: gotowe antologie spoza katalogu bez Ekstraktów (niegotowe – nie); plan dodania zawiera wszystkie
        aktywne teksty z ostrzeżeniami o brakach; dodanie tworzy włączoną antologię na końcu z adresem z formularza;
        antologia już w katalogu, Ekstrakty albo niegotowa – błąd."""
        new = Anthology.objects.create(title='Nowa antologia', status=Anthology.Status.READY)
        text(new, 'Pierwsze', ('Jan', 'Kowalski', ''), genre='fantasy', tags='')
        text(new, 'Drugie', genre='', tags='a, b')
        Anthology.objects.create(title='W przygotowaniu')
        Anthology.objects.create(title='Ekstrakty', is_extracts=True, status=Anthology.Status.READY)
        Anthology.objects.create(title='Powieść w toku', is_novel=True)
        self.assertEqual([item.title for item in candidate_anthologies()], ['Nowa antologia'])
        plan = plan_add(new)
        self.assertEqual([(s['tytul'], s['autor'], s['gatunki'], s['tagi'], s['ostrzezenia']) for s in plan['stories']],
                         [('Drugie', '', [], ['a', 'b'], ['brak gatunku', 'brak autora']),
                          ('Pierwsze', 'Jan Kowalski', ['fantasy'], [], ['brak tagów'])])
        added = apply_add(new, 'https://fantazmaty.pl/czytaj/antologie/nowa/', plan)
        self.assertEqual((added.title, added.url, added.position, added.is_enabled, added.anthology),
                         ('Nowa antologia', 'https://fantazmaty.pl/czytaj/antologie/nowa/', 2, True, new))
        self.assertEqual(list(added.stories.values_list('title', 'position', 'genres', 'tags')),
                         [('Drugie', 1, '', 'a\nb'), ('Pierwsze', 2, 'fantasy', '')])
        self.assertEqual(list(candidate_anthologies()), [])
        for title, message in (('Nowa antologia', 'już w katalogu'), ('Ekstrakty', 'Ekstrakty'),
                               ('W przygotowaniu', 'Gotowa'), ('Powieść w toku', 'Gotowa')):
            with self.subTest(book=title):
                with self.assertRaisesRegex(ValueError, message):
                    plan_add(Anthology.objects.get(title=title))


class CatalogWarningsTests(CatalogSyncDataMixin, TestCase):
    def test_warnings_cover_url_terms_candidates_links_and_duplicates(self):
        """Ostrzeżenia: włączona antologia bez adresu (wyłączona – nie), opowiadania bez gatunku i bez tagów,
        gotowe antologie CMS-u poza katalogiem, wpisy bez powiązania, powtórzone tytuły."""
        no_url = catalog_anthology('Bez adresu', 2, url='')
        story(no_url, 'Pusty', genres='', tags='')
        story(no_url, 'pusty', genres='g', tags='t')
        catalog_anthology('Wyłączona bez adresu', 3, url='', is_enabled=False)
        Anthology.objects.create(title='Kandydatka', status=Anthology.Status.READY)
        self.assertEqual(catalog_warnings(), {
            'no_url': ['Bez adresu'], 'no_genre': ['Bez adresu: Pusty'], 'no_tags': ['Bez adresu: Pusty'],
            'candidates': ['Kandydatka'], 'unlinked_anthologies': ['Bez adresu', 'Wyłączona bez adresu'],
            'unlinked_stories': ['Smocza antologia: Spoza CMS-u', 'Bez adresu: Pusty', 'Bez adresu: pusty'],
            'duplicates': ['Bez adresu: Pusty (2×)'],
        })
