"""Endpoint ``/api/strona/ekipa/``: klucz, treść JSON-a (działy z ról i ręczne, odmiana liczby osób, sortowanie,
pomijanie osób nieaktywnych i zewnętrznych), ślad pobrania i oczyszczanie notki."""
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.site_models import SiteTeamGroup, SiteTeamSection, SiteTeamSettings
from core.site_note import clean_note
from core.inflection import count_text
from core.site_team import PEOPLE_FORMS
from people import roles
from people.models import Person, Role

URL = reverse('core:site_team_api')


def role(name):
    return Role.objects.get_or_create(name=name)[0]


def person(first_name, last_name, *role_names, **fields):
    created = Person.objects.create(first_name=first_name, last_name=last_name,
                                    email=f'{first_name}.{last_name}@example.test'.lower(), **fields)
    created.roles.add(*(role(name) for name in role_names))
    return created


def section(slug, position, **fields):
    return SiteTeamSection.objects.create(slug=slug, title=fields.pop('title', slug.capitalize()),
                                          position=position, **fields)


def roles_group(owner, *role_names, title='', position=1):
    group = SiteTeamGroup.objects.create(section=owner, title=title, position=position,
                                         source=SiteTeamGroup.Source.ROLES)
    group.roles.add(*(role(name) for name in role_names))
    return group


class SiteTeamApiAccessTests(TestCase):
    def test_missing_key_in_settings_gives_503_and_wrong_header_403(self):
        """Bez SITE_API_KEY – 503 z komunikatem; ze złym albo brakującym nagłówkiem – 403; POST – 405."""
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
class SiteTeamApiPayloadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        SiteTeamSection.objects.all().delete()
        cls.editors = section('redakcja', 1, title='Redakcja', navigation_label='Red.')
        cls.editors.coordinator_roles.add(role(roles.EDITING_COORDINATOR))
        roles_group(cls.editors, roles.EDITOR, roles.VERIFIER)
        # Koordynatorzy wpisani ręcznie: za osobami z ról, w podanej kolejności, bez powtórzeń (także z rolą).
        cls.audio = section('audio', 2, title='Dział audio', show_count=False, in_navigation=False,
                            manual_coordinators='Agata Graczyk\n\n Magdalena Świerczek \nAgata Graczyk\n')
        cls.audio.coordinator_roles.add(role(roles.EDITING_COORDINATOR))
        SiteTeamGroup.objects.create(section=cls.audio, title='Lektorzy', position=1,
                                     source=SiteTeamGroup.Source.MANUAL, manual_names='Zenon Lektor\n\n  Anna Głos  \n')
        roles_group(cls.audio, roles.SOUND_ENGINEER, title='Dźwiękowcy', position=2)
        cls.art = section('ilustracje', 3, title='Ilustracje', show_count=False,
                          note='Zobacz <a href="https://fantazmaty.pl/czytaj/">antologie</a> <script>x</script>.')
        cls.off = section('tajny', 4, title='Wyłączony', is_enabled=False)
        roles_group(cls.off, roles.EDITOR)
        person('Magdalena', 'Świerczek', roles.EDITOR, roles.EDITING_COORDINATOR)
        person('Agata', 'Żyła', roles.EDITOR)
        person('Anita', 'Szynal', roles.EDITOR, roles.VERIFIER)  # dwie role jednej grupy – raz
        person('Adam', 'Abacki', roles.VERIFIER)
        person('Bogdan', 'Abacki', roles.VERIFIER)
        person('Dawna', 'Osoba', roles.EDITOR, is_active=False)
        person('Zewnętrzna', 'Osoba', roles.EDITOR, is_external=True)
        person('Teodor', 'Dźwięk', roles.SOUND_ENGINEER)

    def fetch(self):
        response = self.client.get(URL, HTTP_X_SITE_KEY='tajny-klucz')
        self.assertEqual(response.status_code, 200)
        return response

    def test_payload_matches_specification(self):
        """Tylko włączone działy w kolejności; dział z ról: osoby aktywne i niezewnętrzne, bez duplikatów,
        według nazwiska i imienia (polskie znaki jak w alfabecie); ręczny: wiersze w podanej kolejności;
        notka oczyszczona; liczba i jej odmiana tylko przy show_count; koordynacja z ról, a za nią wpisana ręcznie."""
        data = self.fetch().json()
        self.assertEqual(data['prowadzacy'], 'Dawid Wiktorski')
        self.assertEqual(data['strona'], 'Dawid Wiktorski')
        self.assertEqual([item['slug'] for item in data['dzialy']], ['redakcja', 'audio', 'ilustracje'])
        editors, audio, art = data['dzialy']
        self.assertEqual(editors, {
            'slug': 'redakcja', 'kotwica': 'ekipa-redakcja', 'tytul': 'Redakcja', 'w_nawigacji': True,
            'etykieta_nawigacji': 'Red.', 'liczba': 5, 'liczba_tekst': '5 osób',
            'koordynacja': ['Magdalena Świerczek'], 'notka': '',
            'grupy': [{'tytul': '', 'osoby': ['Adam Abacki', 'Bogdan Abacki', 'Anita Szynal', 'Magdalena Świerczek',
                                              'Agata Żyła']}],
        })
        self.assertEqual(audio['etykieta_nawigacji'], 'Dział audio')
        self.assertFalse(audio['w_nawigacji'])
        self.assertIsNone(audio['liczba'])
        self.assertEqual(audio['liczba_tekst'], '')
        self.assertEqual(audio['koordynacja'], ['Magdalena Świerczek', 'Agata Graczyk'])
        self.assertEqual(audio['grupy'], [{'tytul': 'Lektorzy', 'osoby': ['Zenon Lektor', 'Anna Głos']},
                                          {'tytul': 'Dźwiękowcy', 'osoby': ['Teodor Dźwięk']}])
        self.assertEqual(art['grupy'], [])
        self.assertEqual(art['notka'], 'Zobacz <a href="https://fantazmaty.pl/czytaj/">antologie</a> x.')

    def test_response_is_not_cached_and_has_no_emails_or_internal_data(self):
        """Nagłówek no-store, JSON bez ASCII-escape, bez e-maili, kont i ról wewnętrznych; ślad pobrania zapisany."""
        self.assertIsNone(SiteTeamSettings.load().last_fetched_at)
        response = self.fetch()
        self.assertIn('no-store', response['Cache-Control'])
        content = response.content.decode()
        self.assertIn('Świerczek', content)
        for forbidden in ('@', 'example.test', roles.EDITING_COORDINATOR, 'Dawna Osoba', 'Zewnętrzna Osoba',
                          'Wyłączony'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, content)
        settings = SiteTeamSettings.load()
        self.assertIsNotNone(settings.last_fetched_at)
        self.assertEqual(settings.last_fetch_version, response.json()['wersja'])

    def test_names_follow_person_profile_at_once(self):
        """Inicjał dopisany w profilu osoby (i zmiana nazwiska) jest od razu w JSON-ie, także w kolejności."""
        Person.objects.filter(last_name='Żyła').update(first_name='Agata M.', last_name='Abacka')
        editors = self.fetch().json()['dzialy'][0]
        self.assertEqual(editors['grupy'][0]['osoby'], ['Agata M. Abacka', 'Adam Abacki', 'Bogdan Abacki',
                                                         'Anita Szynal', 'Magdalena Świerczek'])

    def test_people_count_text_uses_polish_plural_forms(self):
        """1 osoba, 2–4 osoby, 5+ osób, przy czym 12–14 to „osób”, a 22–24 „osoby”; przy liczbie w dziale
        osoba w dwóch grupach liczy się raz."""
        for count, expected in ((0, '0 osób'), (1, '1 osoba'), (2, '2 osoby'), (4, '4 osoby'), (5, '5 osób'),
                                (12, '12 osób'), (14, '14 osób'), (21, '21 osób'), (22, '22 osoby'), (25, '25 osób'),
                                (112, '112 osób'), (124, '124 osoby')):
            with self.subTest(count=count):
                self.assertEqual(count_text(count, PEOPLE_FORMS), expected)
        roles_group(self.editors, roles.VERIFIER, title='Druga', position=2)
        self.editors.groups.filter(position=1).update(title='Pierwsza')
        editors = self.fetch().json()['dzialy'][0]
        self.assertEqual(editors['liczba'], 5)
        self.assertEqual([len(group['osoby']) for group in editors['grupy']], [5, 3])


class NoteCleaningTests(SimpleTestCase):
    def test_only_links_strong_and_em_survive(self):
        """Zostają <a href="http(s)://…">, <strong>, <em>; inne znaczniki, atrybuty i niebezpieczne adresy znikają,
        a tekst jest escapowany."""
        cases = (
            ('', ''),
            ('zwykły tekst & znak', 'zwykły tekst &amp; znak'),
            ('<strong>a</strong> <em>b</em>', '<strong>a</strong> <em>b</em>'),
            ('<a href="https://x.pl/" target="_blank" onclick="x()">x</a>', '<a href="https://x.pl/">x</a>'),
            ('<a href="javascript:alert(1)">x</a>', 'x'),
            ('<a>bez adresu</a>', 'bez adresu'),
            ('<script>alert(1)</script><b>b</b><p>p</p>', 'alert(1)bp'),
            ('<strong>niedomknięte', '<strong>niedomknięte</strong>'),
            ('<em><strong>zagnieżdżone</em></strong>', '<em><strong>zagnieżdżone</strong></em>'),
            ('a &nbsp; b', 'a   b'),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(clean_note(value), expected)
