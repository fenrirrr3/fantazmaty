"""Zakładka System → „Ekipa WWW”: dostęp tylko dla superusera, link w menu, podgląd działów z ostrzeżeniami,
zapis ustawień i podgląd JSON-a identyczny z endpointem."""
from django.test import TestCase, override_settings
from django.urls import reverse
from lxml import html

from core.site_models import SiteTeamGroup, SiteTeamSection, SiteTeamSettings
from fantazmaty.testing import create_member, create_superuser
from people import roles

from .test_site_team_api import person, role, roles_group, section

URL = reverse('core:site_www')


class SiteWwwAccessTests(TestCase):
    def test_only_superuser_sees_link_and_opens_page(self):
        """Linki „Ekipa WWW”, „Szukajka WWW” i „Słuchaj WWW” w sekcji System tylko dla superusera; koordynator i redaktor dostają 403."""
        cases = ((create_superuser('www-admin'), True), (create_member('www-coordinator', roles.COORDINATOR), False),
                 (create_member('www-editor', roles.EDITOR), False))
        for user, allowed in cases:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                links = html.fromstring(self.client.get(reverse('core:home')).content).xpath(
                    '//nav//details[@data-menu-section="system"]//a[contains(@class, "sidebar-link")]/text()')
                self.assertEqual([link for link in links if link.endswith('WWW')],
                                 ['Ekipa WWW', 'Szukajka WWW', 'Słuchaj WWW'] if allowed else [])
                page = self.client.get(URL)
                self.assertEqual(page.status_code, 200 if allowed else 403)
                self.assertEqual(self.client.get(URL, {'format': 'json'}).status_code, 200 if allowed else 403)
                if allowed:
                    self.assertEqual(html.fromstring(page.content).xpath(f'//nav//a[@href="{URL}"]/@aria-current'),
                                     ['page'])
                else:
                    self.assertEqual(self.client.post(URL, {'lead_editor': 'X', 'site_person': 'Y'}).status_code, 403)


class SiteWwwPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('www-admin')
        SiteTeamSection.objects.all().delete()
        cls.editors = section('redakcja', 1, title='Redakcja')
        cls.editors.coordinator_roles.add(role(roles.EDITING_COORDINATOR))
        roles_group(cls.editors, roles.EDITOR)
        cls.empty = section('korekta', 2, title='Korekta')
        roles_group(cls.empty, roles.PROOFREADER)
        cls.off = section('stare', 3, title='Dawny dział', is_enabled=False)
        SiteTeamGroup.objects.create(section=cls.off, position=1, source=SiteTeamGroup.Source.MANUAL,
                                     manual_names='Ręczna Osoba')
        person('Magdalena', 'Świerczek', roles.EDITOR, roles.EDITING_COORDINATOR)
        person('Jan', 'Poza', roles.VERIFIER, roles.REVIEWER)
        person('Tylko', 'Tłumacz', roles.TRANSLATOR, roles.SOCIAL_MEDIA, roles.EMPEROR)
        person('Sam', 'Koordynator', roles.REVIEW_COORDINATOR)
        person('Dawna', 'Nieaktywna', roles.VERIFIER, is_active=False)

    def setUp(self):
        self.client.force_login(self.admin)

    @override_settings(SITE_API_KEY='')
    def test_preview_shows_sections_sources_and_warnings(self):
        """Podgląd: nazwiska, koordynacja, oznaczenia „z ról”/„ręcznie”, wyłączony dział wyszarzony z adnotacją,
        osoba z rolą bez działu (bez tłumaczy, Social Media, Imperatora, koordynatorów i nieaktywnych),
        dział z ról bez osób, brak klucza i brak pobrania."""
        page = self.client.get(URL)
        self.assertEqual(page.status_code, 200)
        doc = html.fromstring(page.content)
        self.assertEqual(doc.xpath('//section[contains(@class,"site-www-section")]/@id'),
                         ['podglad-redakcja', 'podglad-korekta', 'podglad-stare'])
        self.assertEqual(doc.xpath('//section[contains(@class,"is-disabled")]/@id'), ['podglad-stare'])
        content = page.content.decode()
        for expected in ('Redakcja', '1 osoba', 'Koordynacja:</strong> Magdalena Świerczek', '<li>Magdalena Świerczek</li>',
                         'ręcznie', 'z ról', 'wyłączony – nie trafia na stronę', '<li>Ręczna Osoba</li>',
                         'Brak <code>SITE_API_KEY</code>', 'Strona jeszcze nie pobierała danych.', 'Edytuj działy',
                         reverse('admin:core_siteteamsection_changelist'), '?format=json'):
            with self.subTest(expected=expected):
                self.assertIn(expected, content)
        self.assertEqual(page.context['warnings'], {
            'missing_people': ['Jan Poza (Recenzent, Weryfikator)'], 'empty_sections': ['Korekta']})
        self.assertNotIn('Tylko Tłumacz', content)
        self.assertNotIn('Sam Koordynator', content)
        self.assertNotIn('Dawna Nieaktywna', content)
        self.assertNotIn('@', content.split('<main', 1)[-1].split('class="site-sidebar"')[0])

    @override_settings(SITE_API_KEY='tajny-klucz')
    def test_status_after_fetch_and_without_warnings(self):
        """Po pobraniu przez stronę widać datę; z ustawionym kluczem i bez braków nie ma ostrzeżeń."""
        self.client.get(reverse('core:site_team_api'), HTTP_X_SITE_KEY='tajny-klucz')
        self.off.delete()
        self.empty.delete()
        roles_group(self.editors, roles.VERIFIER, roles.REVIEWER, title='Reszta', position=2)
        page = self.client.get(URL)
        content = page.content.decode()
        self.assertIn('Strona pobrała dane:', content)
        self.assertIn('Ustawiony (<code>SITE_API_KEY</code>)', content)
        self.assertNotIn('Strona jeszcze nie pobierała', content)
        self.assertNotIn('site-www-warnings', content)
        self.assertEqual(page.context['warnings'], {'missing_people': [], 'empty_sections': []})

    def test_warnings_skip_disabled_sections_and_accept_manual_names(self):
        """Osoba tylko w dziale wyłączonym nie trafia na stronę – ostrzeżenie; osoba z rolą wpisana ręcznie
        we włączonym dziale (lista albo koordynacja) – bez ostrzeżenia; wyłączony dział bez osób nie jest „pusty”."""
        roles_group(self.off, roles.TYPESETTER, title='Skład', position=2)
        person('Stefan', 'Składacz', roles.TYPESETTER)
        person('Ręczna', 'Osoba', roles.TYPESETTER)
        self.empty.manual_coordinators = 'Jan Poza'
        self.empty.save()
        SiteTeamGroup.objects.create(section=self.editors, title='Ręcznie', position=2,
                                     source=SiteTeamGroup.Source.MANUAL, manual_names='Ręczna Osoba')
        self.assertEqual(self.client.get(URL).context['warnings'], {
            'missing_people': ['Stefan Składacz (Składacz)'], 'empty_sections': ['Korekta']})

    def test_settings_form_saves_and_rejects_empty_values(self):
        """Zapis prowadzącego i opiekuna strony; puste pole to błąd 400 bez zapisu."""
        response = self.client.post(URL, {'lead_editor': 'Anna Nowa', 'site_person': 'Jan Strona'})
        self.assertRedirects(response, URL)
        settings = SiteTeamSettings.load()
        self.assertEqual((settings.lead_editor, settings.site_person), ('Anna Nowa', 'Jan Strona'))
        response = self.client.post(URL, {'lead_editor': '', 'site_person': 'Jan Strona'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteTeamSettings.load().lead_editor, 'Anna Nowa')
        self.assertContains(self.client.get(URL), 'value="Anna Nowa"')

    @override_settings(SITE_API_KEY='tajny-klucz')
    def test_json_preview_equals_endpoint_without_recording_fetch(self):
        """``?format=json`` daje ten sam JSON co endpoint (z tą samą wersją) i nie zapisuje śladu pobrania."""
        preview = self.client.get(URL, {'format': 'json'})
        self.assertEqual(preview['Content-Type'], 'application/json')
        self.assertIn('no-store', preview['Cache-Control'])
        self.assertIsNone(SiteTeamSettings.load().last_fetched_at)
        self.client.logout()
        live = self.client.get(reverse('core:site_team_api'), HTTP_X_SITE_KEY='tajny-klucz').json()
        expected = preview.json()
        self.assertTrue(expected['wersja'])
        self.assertEqual(live, expected)
        self.assertEqual([item['slug'] for item in expected['dzialy']], ['redakcja', 'korekta'])
