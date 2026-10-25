"""Zakładka System → „Szukajka WWW”: dostęp tylko dla superusera, liczniki i ostrzeżenia, zapis adresu szukajki,
dodanie antologii z CMS-u przez HTTP (podgląd → dodanie), aktualizacja (podgląd różnic → zastosuj), zbiorcza
aktualizacja wszystkich antologii (tylko nowe opowiadania), ``?format=json`` identyczny z endpointem."""
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from lxml import html

from core import site_catalog
from core.site_models import SiteCatalogAnthology, SiteCatalogStory, SiteTeamSettings
from fantazmaty.testing import create_member, create_superuser
from people import roles
from texts.models import Anthology

from .test_site_catalog_api import catalog_anthology, story
from .test_site_catalog_sync import CatalogSyncDataMixin, text

URL = reverse('core:site_catalog')
UPDATE_ALL = reverse('core:site_catalog_update_all')


class SiteCatalogAccessTests(TestCase):
    def test_only_superuser_opens_pages(self):
        """Superuser dostaje 200 (zakładka, JSON, podgląd aktualizacji pojedynczej i zbiorczej); koordynator
        i redaktor – 403 (także POST zbiorczej aktualizacji)."""
        book = Anthology.objects.create(title='Gotowa', status=Anthology.Status.READY)
        update = reverse('core:site_catalog_update', args=[catalog_anthology('Gotowa', 99, anthology=book).pk])
        cases = ((create_superuser('www-admin'), 200), (create_member('www-coordinator', roles.COORDINATOR), 403),
                 (create_member('www-editor', roles.EDITOR), 403))
        for user, expected in cases:
            self.client.force_login(user)
            for name, response in (('zakładka', self.client.get(URL)),
                                   ('json', self.client.get(URL, {'format': 'json'})),
                                   ('aktualizacja', self.client.get(update)),
                                   ('aktualizacja wszystkich', self.client.get(UPDATE_ALL)),
                                   ('zapis adresu', self.client.post(URL, {'action': 'settings',
                                                                           'catalog_page_url': ''}))):
                with self.subTest(user=user.username, page=name):
                    self.assertEqual(response.status_code, 302 if expected == 200 and name == 'zapis adresu' else expected)
            if expected == 403:
                self.assertEqual(self.client.post(UPDATE_ALL, {'plan': 'x'}).status_code, 403)
            if expected == 200:
                page = self.client.get(URL)
                self.assertEqual(html.fromstring(page.content).xpath(f'//nav//a[@href="{URL}"]/@aria-current'), ['page'])


class SiteCatalogPageTests(CatalogSyncDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('www-admin')
        cls.orphan = catalog_anthology('Spoza CMS-u', 2, url='')
        story(cls.orphan, 'Tłumaczenie', 'Obcy Autor', genres='', tags='t')
        Anthology.objects.create(title='Kandydatka', status=Anthology.Status.READY)
        cls.unready = catalog_anthology('Powieść w toku', 3, anthology=Anthology.objects.create(
            title='Powieść w toku', is_novel=True))

    def setUp(self):
        self.client.force_login(self.admin)

    @override_settings(SITE_API_KEY='')
    def test_page_shows_counts_rows_warnings_and_status(self):
        """Liczniki jak w JSON-ie (z gatunkami i tagami z CMS-u), tekst „3 opowiadania · 3 antologie”, wiersz z przyciskiem aktualizacji
        (nieaktywny dla antologii bez powiązania i dla niegotowej powieści – wejście na jej aktualizację wraca
        z komunikatem), znacznik „powieść”, ostrzeżenia, brak klucza i brak pobrania, brak odnośnika
        „Odśwież stronę” bez adresu szukajki."""
        # Katalog (z prefetchami) pobierany raz dla liczników, tabeli i ostrzeżeń.
        with self.assertNumQueries(15):
            page = self.client.get(URL)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['counts'], {'antologie': 3, 'opowiadania': 3, 'gatunki': 5, 'tagi': 6})
        doc = html.fromstring(page.content)
        self.assertEqual(doc.xpath('//dl[contains(@class,"site-catalog-counters")]//dd/text()'), ['3', '3', '5', '6'])
        content = page.content.decode()
        update = reverse('core:site_catalog_update', args=[self.catalog.pk])
        for expected in ('3 opowiadania · 3 antologie', f'href="{update}"', 'Brak <code>SITE_API_KEY</code>',
                         'Strona jeszcze nie pobierała katalogu.', 'Edytuj katalog', '?format=json',
                         reverse('admin:core_sitecataloganthology_changelist'),
                         'Włączone antologie bez adresu strony:</strong> Spoza CMS-u',
                         'Gotowe antologie CMS-u, których nie ma w katalogu:</strong> Kandydatka',
                         'Opowiadania bez gatunku (1)'):
            with self.subTest(expected=expected):
                self.assertIn(expected, content)
        self.assertEqual(doc.xpath('//tr[@id="katalog-%d"]//button[@disabled]/text()' % self.orphan.pk),
                         ['Sprawdź aktualizacje'])
        self.assertEqual(doc.xpath('//tr[@id="katalog-%d"]//button[@disabled]/@title' % self.unready.pk),
                         ['Antologia niegotowa – import z CMS-u po nadaniu jej statusu „Gotowa”.'])
        self.assertEqual(doc.xpath('//tr[@id="katalog-%d"]//span[@class="badge is-info"]/text()'
                                   % self.unready.pk), ['powieść'])
        response = self.client.get(reverse('core:site_catalog_update', args=[self.unready.pk]), follow=True)
        self.assertContains(response, 'Antologia niegotowa – import z CMS-u po nadaniu jej statusu „Gotowa”.')
        self.assertEqual(response.redirect_chain, [(URL, 302)])
        self.assertEqual(doc.xpath('//a[contains(@href, "fz_odswiez")]'), [])
        self.assertNotIn('@', content.split('<main', 1)[-1].split('class="site-sidebar"')[0])

    @override_settings(SITE_API_KEY='tajny-klucz')
    def test_settings_form_saves_page_url_and_shows_refresh_link(self):
        """Zapis adresu szukajki (tylko https) daje odnośnik „Odśwież stronę” z ``?fz_odswiez=1``; po pobraniu przez
        stronę widać datę; adres http – błąd 400 bez zapisu."""
        response = self.client.post(URL, {'action': 'settings', 'catalog_page_url': 'https://fantazmaty.pl/czytaj/szukaj/'})
        self.assertRedirects(response, URL)
        self.assertEqual(SiteTeamSettings.load().catalog_page_url, 'https://fantazmaty.pl/czytaj/szukaj/')
        self.client.get(reverse('core:site_catalog_api'), HTTP_X_SITE_KEY='tajny-klucz')
        content = self.client.get(URL).content.decode()
        self.assertIn('href="https://fantazmaty.pl/czytaj/szukaj/?fz_odswiez=1"', content)
        self.assertIn('Strona pobrała dane:', content)
        self.assertIn('Ustawiony (<code>SITE_API_KEY</code>)', content)
        response = self.client.post(URL, {'action': 'settings', 'catalog_page_url': 'http://fantazmaty.pl/x/'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteTeamSettings.load().catalog_page_url, 'https://fantazmaty.pl/czytaj/szukaj/')

    def test_add_anthology_preview_then_add(self):
        """Select pokazuje tylko kandydatów; podgląd wypisuje opowiadania z ostrzeżeniami i nie zapisuje; dodanie
        z tokenem planu tworzy antologię z opowiadaniami; zły adres – 400; nieaktualny token – 409 bez zapisu."""
        new = Anthology.objects.create(title='Nowa antologia', status=Anthology.Status.READY)
        text(new, 'Pierwsze', ('Jan', 'Kowalski', ''), genre='fantasy', tags='')
        page = self.client.get(URL)
        self.assertEqual([choice[1] for choice in page.context['add_form'].fields['anthology'].choices][1:],
                         ['Kandydatka', 'Nowa antologia'])
        data = {'action': 'preview', 'anthology': new.pk, 'url': 'https://fantazmaty.pl/czytaj/antologie/nowa/'}
        preview = self.client.post(URL, data)
        self.assertEqual(preview.status_code, 200)
        content = preview.content.decode()
        for expected in ('Podgląd: Nowa antologia', '<td>Pierwsze</td>', '<td>Jan Kowalski</td>', 'brak tagów',
                         'Dodaj do katalogu', 'name="plan"'):
            with self.subTest(expected=expected):
                self.assertIn(expected, content)
        self.assertFalse(SiteCatalogAnthology.objects.filter(anthology=new).exists())
        token = html.fromstring(preview.content).xpath('//input[@name="plan"]/@value')[0]
        bad = self.client.post(URL, {**data, 'action': 'add', 'plan': token, 'url': 'https://inna.pl/'})
        self.assertEqual(bad.status_code, 400)
        self.assertContains(bad, 'Adres musi zaczynać się od https://fantazmaty.pl/.', status_code=400)
        text(new, 'Drugie', genre='sf', tags='a')
        stale = self.client.post(URL, {**data, 'action': 'add', 'plan': token})
        self.assertEqual(stale.status_code, 409)
        self.assertFalse(SiteCatalogAnthology.objects.filter(anthology=new).exists())
        token = html.fromstring(stale.content).xpath('//input[@name="plan"]/@value')[0]
        response = self.client.post(URL, {**data, 'action': 'add', 'plan': token}, follow=True)
        self.assertEqual(response.redirect_chain, [(URL, 302)])
        self.assertContains(response, 'Dodano antologię „Nowa antologia” (2 opowiadania).')
        added = SiteCatalogAnthology.objects.get(anthology=new)
        self.assertEqual((added.url, added.position, list(added.stories.values_list('title', flat=True))),
                         ('https://fantazmaty.pl/czytaj/antologie/nowa/', 4, ['Drugie', 'Pierwsze']))

    def test_add_rejects_repeated_and_forged_anthology(self):
        """Drugie „Dodaj do katalogu” (podwójne kliknięcie, stara karta) i podrobione ``anthology`` (Ekstrakty,
        niegotowa, nieistniejąca) – 400 bez zapisu; antologia jest w katalogu raz."""
        new = Anthology.objects.create(title='Nowa antologia', status=Anthology.Status.READY)
        text(new, 'Pierwsze', ('Jan', 'Kowalski', ''), genre='fantasy', tags='a')
        data = {'action': 'preview', 'anthology': new.pk, 'url': 'https://fantazmaty.pl/czytaj/antologie/nowa/'}
        token = html.fromstring(self.client.post(URL, data).content).xpath('//input[@name="plan"]/@value')[0]
        add = {**data, 'action': 'add', 'plan': token}
        self.assertRedirects(self.client.post(URL, add), URL)
        extracts = Anthology.objects.create(title='Ekstrakty', is_extracts=True, status=Anthology.Status.READY)
        before = SiteCatalogAnthology.objects.count()
        for case, anthology in (('ponowne dodanie', new.pk), ('Ekstrakty', extracts.pk),
                                ('niegotowa', Anthology.objects.create(title='Szkic').pk), ('nieistniejąca', 999999)):
            with self.subTest(case=case):
                response = self.client.post(URL, {**add, 'anthology': anthology})
                self.assertEqual(response.status_code, 400)
                self.assertIn('anthology', response.context['add_form'].errors)
        self.assertEqual(SiteCatalogAnthology.objects.count(), before)
        self.assertEqual(SiteCatalogAnthology.objects.filter(anthology=new).count(), 1)

    def test_refresh_link_keeps_query_string_of_page_url(self):
        """Odnośnik „Odśwież stronę” dokleja ``fz_odswiez=1`` przez ``?`` albo ``&`` (adres z parametrami);
        pusty adres – bez odnośnika."""
        site_settings = SiteTeamSettings.load()
        for url, expected in (('', ''),
                              ('https://fantazmaty.pl/czytaj/szukaj/', 'https://fantazmaty.pl/czytaj/szukaj/?fz_odswiez=1'),
                              ('https://fantazmaty.pl/?page_id=7', 'https://fantazmaty.pl/?page_id=7&fz_odswiez=1')):
            with self.subTest(url=url):
                site_settings.catalog_page_url = url
                self.assertEqual(site_settings.catalog_refresh_url, expected)

    def test_update_preview_then_apply(self):
        """Podgląd pokazuje nowe opowiadanie i wpis bez powiązania (bez raportu tytułów i gatunków – są na żywo);
        „Zastosuj” z tokenem dopisuje i wraca do zakładki; drugi podgląd – „Brak zmian”, a POST bez zmian tylko
        informuje."""
        update = reverse('core:site_catalog_update', args=[self.catalog.pk])
        page = self.client.get(update)
        self.assertEqual(page.status_code, 200)
        content = page.content.decode()
        for expected in ('<td>Nowe opowiadanie</td>', 'bez powiązania z tekstem w CMS-ie', 'Zastosuj'):
            with self.subTest(expected=expected):
                self.assertIn(expected, content)
        for absent in ('w CMS-ie tytuł', 'Nowe gatunki i tagi'):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, content)
        token = html.fromstring(page.content).xpath('//input[@name="plan"]/@value')[0]
        self.assertEqual(self.client.post(update, {'plan': 'zły'}).status_code, 409)
        self.assertEqual(self.catalog.stories.count(), 2)
        response = self.client.post(update, {'plan': token}, follow=True)
        self.assertEqual(response.redirect_chain, [(URL, 302)])
        self.assertContains(response, '„Smocza antologia”: dodano 1 opowiadanie.')
        self.assertEqual(self.catalog.stories.count(), 3)
        again = self.client.get(update)
        self.assertContains(again, 'Brak zmian')
        self.assertNotContains(again, 'name="plan"')
        self.assertContains(self.client.post(update, {}, follow=True), 'brak zmian do zastosowania')
        self.assertEqual(self.catalog.stories.count(), 3)
        self.assertEqual(self.client.get(reverse('core:site_catalog_update', args=[999999])).status_code, 404)

    @override_settings(SITE_API_KEY='tajny-klucz')
    def test_json_preview_equals_endpoint_without_recording_fetch(self):
        """``?format=json`` daje ten sam JSON co endpoint (z tą samą wersją) i nie zapisuje śladu pobrania."""
        preview = self.client.get(URL, {'format': 'json'})
        self.assertEqual(preview['Content-Type'], 'application/json')
        self.assertIn('no-store', preview['Cache-Control'])
        self.assertIsNone(SiteTeamSettings.load().catalog_fetched_at)
        self.client.logout()
        live = self.client.get(reverse('core:site_catalog_api'), HTTP_X_SITE_KEY='tajny-klucz').json()
        expected = preview.json()
        self.assertTrue(expected['wersja'])
        self.assertEqual(live, expected)
        self.assertEqual([item['nazwa'] for item in expected['antologie']],
                         ['Smocza antologia', 'Spoza CMS-u', 'Powieść w toku'])


class SiteCatalogUpdateAllTests(CatalogSyncDataMixin, TestCase):
    """Zbiorcza aktualizacja: nowe opowiadanie w jednej antologii, dwie bez zmian (nowy tag w CMS-ie to nie zmiana
    – jest na żywo), pominięte niepowiązana i niegotowa."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('www-admin')
        second = Anthology.objects.create(title='Druga gotowa', status=Anthology.Status.READY)
        cls.second = catalog_anthology('Druga gotowa', 2, anthology=second)
        story(cls.second, 'Drugie', 'Ewa Lis', genres='sf', tags='kosmos',
              text=text(second, 'Drugie', ('Ewa', 'Lis', ''), genre='sf', tags='kosmos, nowy-tag'))
        same = Anthology.objects.create(title='Zgodna', status=Anthology.Status.READY)
        cls.same = catalog_anthology('Zgodna', 3, anthology=same)
        story(cls.same, 'Zgodne', 'Iza Sowa', genres='fantasy', tags='las',
              text=text(same, 'Zgodne', ('Iza', 'Sowa', ''), genre='fantasy', tags='las'))
        cls.orphan = catalog_anthology('Spoza CMS-u', 4)
        cls.unready = catalog_anthology('W przygotowaniu', 5, anthology=Anthology.objects.create(title='W przygotowaniu'))
        cls.second_book = second

    def setUp(self):
        self.client.force_login(self.admin)

    def snapshot(self):
        return list(SiteCatalogStory.objects.order_by('pk').values_list('pk', 'genres', 'tags'))

    def test_tab_button_and_preview_group_anthologies_without_saving(self):
        """Zakładka ma przycisk „Sprawdź aktualizacje wszystkich”; podgląd: antologia z nowym opowiadaniem,
        „Druga gotowa” (tylko nowy tag w CMS-ie) i „Zgodna” bez zmian, niepowiązana i niegotowa pominięte z powodem;
        nic nie zapisane."""
        tab = html.fromstring(self.client.get(URL).content)
        self.assertEqual(tab.xpath(f'//a[@href="{UPDATE_ALL}"]/text()'), ['Sprawdź aktualizacje wszystkich'])
        before = self.snapshot()
        page = self.client.get(UPDATE_ALL)
        self.assertEqual(page.status_code, 200)
        overview = page.context['overview']
        self.assertEqual([plan['anthology'].title for plan in overview['changed']], ['Smocza antologia'])
        self.assertEqual([plan['anthology'].title for plan in overview['unchanged']], ['Druga gotowa', 'Zgodna'])
        self.assertEqual([(item['anthology'].title, item['reason']) for item in overview['skipped']], [
            ('Spoza CMS-u', 'Antologia nie jest powiązana z CMS-em – powiąż ją w panelu administracyjnym.'),
            ('W przygotowaniu', 'Antologia niegotowa – import z CMS-u po nadaniu jej statusu „Gotowa”.')])
        content = page.content.decode()
        for expected in ('<td>Nowe opowiadanie</td>', 'nowe opowiadania: 1', 'Antologie zgodne z CMS-em (2)',
                         'Zastosuj wszystkie',
                         'panelu administracyjnym.</strong> (1): Spoza CMS-u',
                         'statusu „Gotowa”.</strong> (1): W przygotowaniu'):
            with self.subTest(expected=expected):
                self.assertIn(expected, content)
        self.assertEqual(self.snapshot(), before)

    def test_apply_all_in_one_transaction_with_counters_and_stale_preview(self):
        """Nieaktualny podgląd (nowy tekst w CMS-ie) – 409 bez zapisu; błąd w trakcie – wszystko wycofane;
        aktualny token stosuje oba plany z licznikami w komunikacie; potem „brak zmian”."""
        token = html.fromstring(self.client.get(UPDATE_ALL).content).xpath('//input[@name="plan"]/@value')[0]
        text(self.second_book, 'Dopisane później', ('Ola', 'Kos', ''), genre='horror', tags='mgła')
        before = self.snapshot()
        stale = self.client.post(UPDATE_ALL, {'plan': token})
        self.assertEqual(stale.status_code, 409)
        self.assertContains(stale, 'Dane w CMS-ie zmieniły się od podglądu', status_code=409)
        self.assertEqual(self.snapshot(), before)
        token = html.fromstring(stale.content).xpath('//input[@name="plan"]/@value')[0]
        real_apply = site_catalog.apply_update
        calls = []

        def failing_second(plan):
            calls.append(plan['anthology'].title)
            if len(calls) == 2:
                raise RuntimeError('awaria w trakcie')
            return real_apply(plan)

        with (mock.patch.object(site_catalog, 'apply_update', failing_second), self.assertRaises(RuntimeError),
              self.assertLogs('django.request', 'ERROR')):
            self.client.post(UPDATE_ALL, {'plan': token})
        self.assertEqual((calls, self.snapshot()), (['Smocza antologia', 'Druga gotowa'], before))
        response = self.client.post(UPDATE_ALL, {'plan': token}, follow=True)
        self.assertEqual(response.redirect_chain, [(URL, 302)])
        self.assertContains(response, 'Zaktualizowano katalog w 2 antologiach: dodano 2 opowiadania.')
        self.assertEqual(list(self.second.stories.values_list('title', flat=True)), ['Drugie', 'Dopisane później'])
        self.assertEqual(self.catalog.stories.count(), 3)
        again = self.client.get(UPDATE_ALL)
        self.assertEqual(again.context['overview']['changed'], [])
        self.assertNotContains(again, 'name="plan"')
        self.assertContains(self.client.post(UPDATE_ALL, {}, follow=True), 'Brak zmian do zastosowania')
