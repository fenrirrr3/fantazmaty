"""Słuchaj WWW – endpoint ``/api/strona/sluchaj/`` (klucz, ślad pobrania), zakładka System → „Słuchaj WWW” (dostęp,
liczniki, ostrzeżenia, adres strony, dodanie antologii, ponowne dopasowanie, JSON) i panel admina antologii z czasami."""
from django.contrib import admin
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from lxml import html

from core.site_listen import listen_payload
from core.site_models import SiteListenAnthology, SiteListenRecording, SiteTeamSettings
from fantazmaty.testing import create_member, create_superuser
from people import roles
from texts.models import Anthology

from .test_site_listen_payload import COVER, ListenDataMixin, published

API = reverse('core:site_listen_api')
URL = reverse('core:site_listen')


class SiteListenApiTests(ListenDataMixin, TestCase):
    def test_key_errors(self):
        """Bez SITE_API_KEY – 503; bez nagłówka, ze złym kluczem albo jego prefiksem – 403; POST – 405."""
        with override_settings(SITE_API_KEY=''):
            response = self.client.get(API, HTTP_X_SITE_KEY='cokolwiek')
            self.assertEqual((response.status_code, response.json()), (503, {'blad': 'Brak klucza SITE_API_KEY w konfiguracji.'}))
        with override_settings(SITE_API_KEY='tajny-klucz'):
            for case, headers in (('brak nagłówka', {}), ('zły klucz', {'HTTP_X_SITE_KEY': 'inny'}),
                                  ('prefiks', {'HTTP_X_SITE_KEY': 'tajny'})):
                with self.subTest(case):
                    response = self.client.get(API, **headers)
                    self.assertEqual((response.status_code, response.json()), (403, {'blad': 'Brak dostępu.'}))
            self.assertEqual(self.client.post(API, HTTP_X_SITE_KEY='tajny-klucz').status_code, 405)
        self.assertIsNone(SiteTeamSettings.load().listen_fetched_at)

    @override_settings(SITE_API_KEY='tajny-klucz')
    def test_payload_no_store_and_fetch_record(self):
        """Odpowiedź = ``listen_payload`` (bez ASCII-escape, no-store), zapis daty i wersji pobrania nagrań – osobno
        od śladów ekipy i katalogu."""
        response = self.client.get(API, HTTP_X_SITE_KEY='tajny-klucz')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        data = response.json()
        self.assertEqual(data, listen_payload())
        self.assertIn('Przykładowa', response.content.decode())
        settings = SiteTeamSettings.load()
        self.assertEqual(settings.listen_fetch_version, data['wersja'])
        self.assertIsNotNone(settings.listen_fetched_at)
        self.assertIsNone(settings.catalog_fetched_at)
        self.assertIsNone(settings.last_fetched_at)


class SiteListenPageTests(ListenDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('www-admin')
        cls.candidate = Anthology.objects.create(title='Gastronomicon', status=Anthology.Status.READY)
        published(cls.candidate, 'Danie', youtube='https://youtu.be/danie')
        published(cls.book, 'Bez linków')
        published(cls.book, 'Bez daty', youtube='https://youtu.be/bez-daty', premiere=None)

    def test_only_superuser(self):
        """Superuser – 200 (zakładka, JSON) i podświetlona pozycja menu; koordynator i redaktor – 403 (także POST)."""
        for user, expected in ((self.admin, 200), (create_member('www-coordinator', roles.COORDINATOR), 403),
                               (create_member('www-editor', roles.EDITOR), 403)):
            self.client.force_login(user)
            for name, response in (('zakładka', self.client.get(URL)), ('json', self.client.get(URL, {'format': 'json'})),
                                   ('dopasowanie', self.client.post(URL, {'action': 'relink'}))):
                with self.subTest(user=user.username, page=name):
                    self.assertEqual(response.status_code, 302 if expected == 200 and name == 'dopasowanie' else expected)
        self.client.force_login(self.admin)
        doc = html.fromstring(self.client.get(URL).content)
        self.assertEqual(doc.xpath(f'//nav//a[@href="{URL}"]/@aria-current'), ['page'])

    @override_settings(SITE_API_KEY='')
    def test_counts_warnings_status_and_links(self):
        """Liczniki (antologie, nagrania, YT, HT, bez czasu, bez twórców), „5 nagrań”, ostrzeżenia (m.in. brak daty
        premiery), brak klucza
        i pobrania, odnośnik „Odśwież stronę” z ?fz_odswiez=1, admin, JSON identyczny z endpointem."""
        SiteListenRecording.objects.create(listen_anthology=self.entry, card_id='fza-1', source_title='Stare nagranie')
        self.client.force_login(self.admin)
        page = self.client.get(URL)
        self.assertEqual(page.status_code, 200)
        doc = html.fromstring(page.content)
        self.assertEqual(doc.xpath('//dl[contains(@class,"site-catalog-counters")]//dd/text()'), ['2', '5', '4', '3', '3', '1'])
        content = page.content.decode()
        for expected in ('„5 nagrań”', 'Brak <code>SITE_API_KEY</code>', 'Strona jeszcze nie pobierała nagrań.',
                         'href="https://fantazmaty.pl/sluchaj/?fz_odswiez=1"', 'Antologia słuchana: Bez linków',
                         'Gastronomicon (1 nagranie)', 'brak daty premiery</strong> (pominięte): Antologia słuchana: Bez daty', 'Nagrania bez czasu (3)',
                         'Nagrania z dawnej strony bez audiobooka w CMS-ie (1)', 'Stare nagranie (fza-1)',
                         'Dopasuj ponownie', reverse('admin:core_sitelistenanthology_changelist'), '&quot;fza-123&quot;'):
            with self.subTest(expected=expected):
                self.assertIn(expected, content)
        data = self.client.get(URL, {'format': 'json'}).json()
        self.assertEqual(data['antologie'], listen_payload()['antologie'])
        self.assertIsNone(SiteTeamSettings.load().listen_fetched_at, 'podgląd nie jest pobraniem przez stronę')

    def test_settings_add_and_relink_actions(self):
        """Zapis adresu strony (https), dodanie antologii z CMS-u na koniec z okładką z fantazmaty.pl (identyfikator
        z nazwy), odrzucenie okładki spoza strony i antologii spoza kandydatów, ponowne dopasowanie nagrań."""
        self.client.force_login(self.admin)
        response = self.client.post(URL, {'action': 'settings', 'listen_page_url': 'https://fantazmaty.pl/sluchaj/nowa/'})
        self.assertRedirects(response, URL)
        self.assertEqual(SiteTeamSettings.load().listen_refresh_url, 'https://fantazmaty.pl/sluchaj/nowa/?fz_odswiez=1')
        for label, data in (('okładka spoza strony', {'anthology': self.candidate.pk, 'cover_url': 'https://example.com/a.jpg'}),
                            ('antologia już na stronie', {'anthology': self.book.pk, 'cover_url': COVER}),
                            ('zły adres strony', None)):
            with self.subTest(label):
                if data is None:
                    response = self.client.post(URL, {'action': 'settings', 'listen_page_url': 'http://fantazmaty.pl/'})
                else:
                    response = self.client.post(URL, {'action': 'add', **data})
                self.assertEqual(response.status_code, 400)
        response = self.client.post(URL, {'action': 'add', 'anthology': self.candidate.pk, 'cover_url': COVER})
        self.assertRedirects(response, URL)
        added = SiteListenAnthology.objects.get(anthology=self.candidate)
        self.assertEqual((added.slug, added.title, added.cover_url, added.position), ('gastronomicon', 'Gastronomicon', COVER, 3))
        self.assertEqual(listen_payload()['antologie'][-1]['id'], 'gastronomicon')
        self.assertContains(self.client.get(URL), 'Wszystkie antologie z opublikowanymi audiobookami są już na stronie.')
        stray = SiteListenRecording.objects.create(listen_anthology=self.entry, card_id='fza-77',
                                                   source_youtube='https://youtube.com/watch?v=danie')
        response = self.client.post(URL, {'action': 'relink'}, follow=True)
        self.assertContains(response, 'Dopasowano: 0 antologii, 1 nagranie.')
        stray.refresh_from_db()
        self.assertEqual(stray.audiobook.text.title, 'Danie')


class SiteListenAdminTests(ListenDataMixin, TestCase):
    def test_admin_group_screens_and_inline(self):
        """Antologie strony „Słuchaj” są w grupie „Ustawienia” pod polskim adresem; lista i edycja z inline nagrań
        (czasy, id karty, dane z dawnej strony) otwierają się superuserowi; ustawienia mają adres strony."""
        user = create_superuser('www-admin')
        request = RequestFactory().get(reverse('admin:index'))
        request.user = user
        groups = {group['app_label']: group for group in admin.site.get_app_list(request)}
        names = {model['object_name'].lower(): model['name'] for model in groups['settings']['models']}
        self.assertEqual(names['sitelistenanthology'], 'Słuchaj WWW – antologie i czasy nagrań')
        SiteListenRecording.objects.create(listen_anthology=self.entry, card_id='fza-5', source_title='Z dawnej',
                                           source_author='Autorka')
        change = reverse('admin:core_sitelistenanthology_change', args=[self.entry.pk])
        self.assertEqual(change, f'/panel/organizacja/sluchaj-www-antologie/{self.entry.pk}/edytuj/')
        self.client.force_login(user)
        page = self.client.get(change)
        self.assertEqual(page.status_code, 200)
        for expected in ('name="recordings-TOTAL_FORMS"', 'value="41:11"', 'value="fza-123"', 'Z dawnej – Autorka',
                         reverse('core:site_listen')):
            with self.subTest(expected=expected):
                self.assertContains(page, expected)
        self.assertContains(self.client.get(reverse('admin:core_sitelistenanthology_changelist')), 'antologia-sluchana')
        self.assertContains(self.client.get(reverse('admin:core_siteteamsettings_change', args=[1])), 'listen_page_url')
        self.client.force_login(create_member('www-coordinator', roles.COORDINATOR))
        self.assertIn(self.client.get(change).status_code, (302, 403))
