"""Strona programów: zwarte karty, dymki „?” z opisami, strona stanu zadania, sekcje superusera
(scalanie, podział, stopki tekstów antologii)."""
import re
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.services import program_jobs as jobs
from fantazmaty.testing import create_member, create_superuser

from .support import IsolatedProgramDirsMixin, upload


STEP = re.compile(r'^(?P<tag>[a-z0-9]*)(?P<rest>.*)$')
PART = re.compile(r'#(?P<id>[\w-]+)|\.(?P<cls>[\w-]+)|\[(?P<attr>[\w-]+)(?:=(?P<value>[^\]]+))?\]')


def css(node, selector):
    """Proste selektory CSS (znacznik, #id, .klasa, [atrybut=wartość], potomek) bez pakietu cssselect."""
    path = '.'
    for step in selector.split():
        match = STEP.match(step)
        tests = []
        for part in PART.finditer(match['rest']):
            if part['id']:
                tests.append(f"@id='{part['id']}'")
            elif part['cls']:
                tests.append(f"contains(concat(' ', normalize-space(@class), ' '), ' {part['cls']} ')")
            elif part['value']:
                tests.append(f"@{part['attr']}='{part['value']}'")
            else:
                tests.append(f"@{part['attr']}")
        path += '//' + (match['tag'] or '*') + ''.join(f'[{test}]' for test in tests)
    return node.xpath(path)


def text_of(node):
    return re.sub(r'\s+', ' ', node.text_content()).strip()


class ProgramsPageLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('wyglad-programow', 'Redaktor')

    def setUp(self):
        self.client.force_login(self.member)
        self.response = self.client.get(reverse('core:programs'))
        self.tree = html.fromstring(self.response.content.decode())

    def tips(self):
        return css(self.tree, 'details.help-tip')

    def tip_text(self):
        return ' '.join(text_of(body) for body in css(self.tree, '.help-tip-body'))

    def visible_text(self):
        """Tekst strony poza dymkami (to, co użytkownik widzi bez rozwijania „?”)."""
        main = css(self.tree, 'main')[0]
        for body in css(main, '.help-tip-body'):
            body.drop_tree()
        return text_of(main)

    def test_each_program_is_a_card_with_short_title_and_one_sentence(self):
        cards = css(self.tree, 'details.program-tool')
        self.assertEqual([card.get('id') for card in cards], ['program-clean', 'program-convert', 'program-repetitions'])
        for card in cards:
            with self.subTest(card=card.get('id')):
                summary = css(card, 'summary')[0]
                self.assertTrue(text_of(css(summary, '.program-title')[0]))
                lead = text_of(css(summary, '.program-lead')[0])
                self.assertEqual(lead.count('.'), 1, lead)
                # Plik i przycisk akcji stoją na początku karty, przed opcjami.
                main = css(card, '.program-main')[0]
                self.assertTrue(css(main, 'input[type=file]'))
                self.assertTrue(css(main, 'button[type=submit]'))

    def test_long_descriptions_moved_to_help_tips(self):
        tips = self.tip_text()
        visible = self.visible_text()
        for text in ('zaakceptuj śledzone zmiany', 'Times New Roman 12 pkt', 'Przebudowa tworzy nowy DOCX',
                     'Recenzja → Zaakceptuj wszystkie zmiany', 'Podsumowanie pojawi się obok uwag',
                     'Jeden format pobierzesz jako osobny plik', 'spis treści z Nagłówków 1–3',
                     'Uwagi konwertera', 'Konwersja może potrwać do 90 sekund',
                     'Zakres określa liczbę analizowanych słów', 'Powtórzenia mają kolorowe litery',
                     'Wyjątki mają pierwszeństwo', 'Kolory mogą różnić się przy kolejnych uruchomieniach',
                     'Domyślnie wyłączone jest tylko scalanie pustych akapitów',
                     'Działają tylko z zaznaczoną opcją „Użyj Odkurzacza”',
                     'Oryginał pozostaje na Twoim komputerze', 'Miękkie entery (Shift+Enter)',
                     'Paleta kolorów liter', 'rozmiar strony, orientację, marginesy',
                     'po pobraniu przejrzyj wynik'):
            with self.subTest(text=text):
                self.assertIn(text, tips)
                self.assertNotIn(text, visible)

    def test_tips_describe_kept_review_markup_and_linked_images(self):
        """Pomoc mówi, że komentarze i śledzone zmiany zostają (z nazwiskami autorów), a obrazy połączone znikają
        z linkiem."""
        tips = self.tip_text()
        for text in ('komentarze (z odpowiedziami) i śledzone zmiany zostają',
                     'w pliku zostają więc nazwiska autorów komentarzy i zmian',
                     'Komentarze i śledzone zmiany zostają w pliku (z nazwiskami autorów i datami)',
                     'tekst po przyjęciu śledzonych zmian, bez komentarzy',
                     'obrazy połączone z plikami spoza dokumentu – razem z linkiem'):
            with self.subTest(text=text):
                self.assertIn(text, tips)

    def test_tips_inform_about_removed_images_and_polish_language(self):
        for card in css(self.tree, 'details.program-tool'):
            with self.subTest(card=card.get('id')):
                text = ' '.join(text_of(body) for body in css(card, '.help-tip-body'))
                self.assertIn('obrazy', text)
                self.assertIn('język polski', text)

    def test_help_tips_are_accessible(self):
        self.assertGreaterEqual(len(self.tips()), 12)
        ids = self.tree.xpath('//@id')
        self.assertEqual(len(ids), len(set(ids)), 'Powtórzone id na stronie')
        for tip in self.tips():
            summary = css(tip, 'summary')[0]
            self.assertTrue(summary.get('aria-label', '').startswith('Pomoc: '))
            self.assertEqual(text_of(summary), '?')
            self.assertTrue(text_of(css(tip, '.help-tip-body')[0]))
            # Dymek nie może być w etykiecie pola (kliknięcie przełączałoby pole).
            self.assertFalse(tip.xpath('ancestor::label'))
        for element in self.tree.xpath('//*[@aria-describedby]'):
            for target in element.get('aria-describedby').split():
                self.assertTrue(self.tree.get_element_by_id(target, None) is not None, target)

    def test_soft_whitespace_is_in_the_rule_group_with_select_all(self):
        group = css(self.tree, '#program-clean fieldset.program-rules')[0]
        self.assertTrue(text_of(css(group, 'legend')[0]).startswith('Opcje korekty'))
        boxes = css(group, '.odkurzacz-rules input[type=checkbox]')
        self.assertIn('remove_soft_whitespace', [box.get('name') for box in boxes])
        self.assertEqual(len(css(group, '[data-rules]')), 2)

    def test_advanced_options_are_collapsible_and_open_when_used(self):
        advanced = css(self.tree, '#program-clean details.program-advanced')[0]
        names = [box.get('name') for box in css(advanced, 'input[type=checkbox]')]
        self.assertEqual(names, ['normalize_formatting', 'rebuild', 'keep_page_format'])
        self.assertIsNone(advanced.get('open'))
        with patch('core.views.programs.stored_preferences', return_value={'clean': {'rebuild': True}}):
            tree = html.fromstring(self.client.get(reverse('core:programs')).content.decode())
        self.assertIsNotNone(css(tree, '#program-clean details.program-advanced')[0].get('open'))

    def test_dictionary_link_and_reset_stay_visible(self):
        visible = self.visible_text()
        self.assertIn('Słownik zamian', visible)
        self.assertEqual(len(css(self.tree, 'form.program-reset')), 3)


class SuperuserProgramsPageTests(TestCase):
    def test_merge_and_split_tips_describe_review_markup(self):
        """Scalanie i podział (tylko superuser): komentarze i śledzone zmiany zostają, obrazy połączone znikają z linkiem."""
        self.client.force_login(create_superuser('pomoc-admin'))
        tree = html.fromstring(self.client.get(reverse('core:programs')).content.decode())
        tips = ' '.join(text_of(body) for body in css(tree, '.help-tip-body'))
        for text in ('Przebudowa zachowuje przypisy, komentarze i śledzone zmiany (z nazwiskami autorów i datami)',
                     'komentarz trafia do pliku ze swoim odwołaniem',
                     'Obrazy połączone z plikami spoza dokumentu znikają razem z linkiem'):
            with self.subTest(text=text):
                self.assertIn(text, tips)

    def test_superuser_limits_are_in_tips(self):
        admin = create_superuser('wyglad-admin')
        self.client.force_login(admin)
        tree = html.fromstring(self.client.get(reverse('core:programs')).content.decode())
        tips = ' '.join(text_of(body) for body in css(tree, '.help-tip-body'))
        self.assertIn('nie masz limitu wielkości analizy', tips)
        self.assertIn(f'do {jobs.superuser_time_limit()} sekund', tips)


class SuperuserProgramSectionsTests(IsolatedProgramDirsMixin, TestCase):
    """Sekcje 4–6 (scalanie, podział, stopki) tylko dla superusera – w szablonie i po stronie serwera."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('sekcje-admin')
        cls.coordinator = create_member('sekcje-koordynator', 'Koordynator')

    def page(self, user, **params):
        self.client.force_login(user)
        response = self.client.get(reverse('core:programs'), params)
        return response, html.fromstring(response.content.decode())

    def test_lead_texts_mention_editorial_gremlins(self):
        """Opis strony i karty Odkurzacza: spacje, „edytorskie chochliki” i zapis tekstu."""
        _, tree = self.page(self.coordinator)
        self.assertEqual(text_of(css(tree, '.page-description')[0]).split('.')[0],
                         'Popraw spacje, edytorskie chochliki i zapis tekstu w dokumencie Word')
        self.assertEqual(text_of(css(tree, '#program-clean .program-lead')[0]),
                         'Poprawia spacje, edytorskie chochliki i zapis tekstu w dokumencie DOCX.')

    def test_superuser_sees_six_sections_coordinator_three(self):
        """Superuser widzi scalanie, podział i stopki; koordynator tylko trzy dotychczasowe programy."""
        for user, expected in ((self.admin, ['program-clean', 'program-convert', 'program-repetitions',
                                             'program-merge', 'program-split', 'program-credits']),
                               (self.coordinator, ['program-clean', 'program-convert', 'program-repetitions'])):
            with self.subTest(user=user.username):
                response, tree = self.page(user)
                self.assertEqual([card.get('id') for card in css(tree, 'details.program-tool')], expected)
                ids = tree.xpath('//@id')
                self.assertEqual(len(ids), len(set(ids)), 'Powtórzone id na stronie')
        _, tree = self.page(self.admin)
        merge_input = css(tree, '#program-merge input[type=file]')[0]
        self.assertIsNotNone(merge_input.get('multiple'))
        self.assertTrue(css(tree, '#program-merge ol[data-file-list]'))
        self.assertIsNone(css(tree, '#program-credits')[0].get('open'))

    def test_template_styles_checkbox_only_in_superuser_merge_section(self):
        """Pole „Użyj stylów szablonu Fantazmatów” jest w sekcji scalania (odznaczone, z dymkiem „?”), koordynator go nie widzi."""
        response, tree = self.page(self.admin)
        checkbox = css(tree, '#program-merge input[name=merge-use_template]')[0]
        self.assertEqual((checkbox.get('type'), checkbox.get('checked')), ('checkbox', None))
        check = checkbox.getparent().getparent()
        self.assertEqual(text_of(check.find('label')), 'Użyj stylów szablonu Fantazmatów')
        tip = text_of(css(check, '.help-tip-body')[0])
        self.assertIn('TEKST Szablon 10-2025', tip)
        self.assertIn('z szablonu nie jest przenoszone nic poza stylami', tip)
        self.assertEqual(css(check, '.help-tip-body')[0].get('id'), checkbox.get('aria-describedby'))
        response, _ = self.page(self.coordinator)
        self.assertNotIn('use_template', response.content.decode())

    def test_coordinator_gets_403_for_superuser_programs(self):
        """Akcje scalania i podziału, reset ich ustawień i stopki odrzucają koordynatora (403), bez zadania."""
        self.client.force_login(self.coordinator)
        url = reverse('core:programs')
        requests = (
            ({'program_action': 'merge', 'merge-documents': [upload(), upload(name='b.docx')]}, {}),
            ({'program_action': 'merge', 'merge-documents': [upload(), upload(name='b.docx')]},
             {'HTTP_X_PROGRAM_JOB': '1'}),
            ({'program_action': 'split', 'split-document': upload()}, {}),
            ({'program_action': 'split', 'split-document': upload()}, {'HTTP_X_PROGRAM_JOB': '1'}),
            ({'program_action': 'reset_settings', 'program': 'merge'}, {}),
        )
        for data, headers in requests:
            with self.subTest(action=data['program_action'], json=bool(headers)), patch.object(jobs, 'launch') as launch:
                response = self.client.post(url, data, **headers)
                self.assertEqual(response.status_code, 403)
                launch.assert_not_called()
        self.assertEqual(self.client.get(url, {'antologia': '1'}).status_code, 403)
        self.assertFalse(jobs.root().exists() and any(jobs.root().iterdir()))

    def test_superuser_merge_and_split_create_jobs(self):
        """Superuser uruchamia scalanie (lista plików w kolejności wyboru) i podział jako zadania w tle."""
        self.client.force_login(self.admin)
        for data, expected in (
            ({'program_action': 'merge', 'merge-documents': [upload(name='b.docx'), upload(name='a.docx')]},
             ['b.docx', 'a.docx']),
            ({'program_action': 'split', 'split-document': upload()}, None),
        ):
            with self.subTest(action=data['program_action']), patch.object(jobs, 'launch') as launch:
                response = self.client.post(reverse('core:programs'), data, HTTP_X_PROGRAM_JOB='1')
                self.assertEqual(response.status_code, 202, response.content)
                launch.assert_called_once()
                folder = launch.call_args.args[0]
                request = jobs.read_json(folder / 'request.json')
                self.assertEqual(request['action'], data['program_action'])
                self.assertEqual(request.get('names'), expected)
                if expected:
                    self.assertEqual(sorted(path.name for path in (folder / 'sources').iterdir()), ['01.docx', '02.docx'])
                    self.assertTrue(request['options']['rebuild'])
                jobs.write_json(folder / 'state.json', {'state': 'cancelled'})


class AnthologyCreditsTests(TestCase):
    """Stopki tekstów antologii: kolejność, autorzy, treść jak na karcie tekstu, bez wycofanych."""

    @classmethod
    def setUpTestData(cls):
        from authors.models import Author
        from core.supervision import text_credit_groups
        from fantazmaty.testing.history import import_completed_workflow
        from people.models import Person
        from texts.models import Anthology, Text
        from workflow.models import WorkflowStage

        cls.admin = create_superuser('stopki-admin')
        cls.book = Anthology.objects.create(title='Antologia stopek')
        Anthology.objects.create(title='Inna antologia')
        cls.editor = Person.objects.create(user=cls.admin, email='stopki@example.com', first_name='Ala', last_name='Redaktor')
        authors = [Author.objects.create(first_name='Jan', last_name='Kowalski', email='jan@example.test'),
                   Author.objects.create(first_name='Anna', last_name='Nowak', pseudonym='A. N. Pióro',
                                         email='anna@example.test')]
        cls.texts = {}
        for title, writers in (('Zmierzch', authors[:1]), ('Brzask', authors), ('Wycofany', authors[:1])):
            text = Text.objects.create(title=title, anthology=cls.book, length=100)
            text.authors.add(*writers)
            cls.texts[title] = text
        import_completed_workflow(text_id=cls.texts['Brzask'].pk, next_stage='ready', stages=[
            {'stage_type': 'editing', 'assigned_to': cls.editor.email}])
        WorkflowStage.objects.create(text=cls.texts['Wycofany'], stage_type='withdrawn', is_released=True,
                                     is_completed=False)
        cls.expected_groups = [group for group in text_credit_groups(cls.texts['Brzask']) if group['names']]

    def setUp(self):
        self.client.force_login(self.admin)
        self.response = self.client.get(reverse('core:programs'), {'antologia': self.book.pk})
        self.tree = html.fromstring(self.response.content.decode())

    def test_withdrawn_text_skipped_and_order_by_title(self):
        """Wycofany tekst nie pojawia się; kolejność jak na liście tekstów antologii (tytuł)."""
        self.assertIsNotNone(css(self.tree, '#program-credits')[0].get('open'))
        titles = [text_of(node) for node in css(self.tree, '.program-credits-item h3')]
        self.assertEqual(titles, ['Brzask', 'Zmierzch'])
        self.assertNotIn('Wycofany', css(self.tree, '#program-credits-text')[0].text)
        options = [text_of(option) for option in css(self.tree, '#program-credits-anthology option')][1:]
        self.assertEqual(options, ['Antologia stopek', 'Inna antologia'])

    def test_copy_block_has_authors_and_the_same_credits_as_the_text_card(self):
        """Blok do skopiowania: „Tytuł – autorzy” (pseudonim zamiast nazwiska) i niepuste wiersze stopki z karty tekstu."""
        self.assertTrue(self.expected_groups, 'Dane testu muszą mieć niepustą stopkę')
        lines = [f'{group["label"]}: {", ".join(group["names"])}' for group in self.expected_groups]
        expected = '\n'.join(['Brzask – Jan Kowalski, A. N. Pióro', *lines]) + '\n\nZmierzch – Jan Kowalski'
        self.assertEqual(css(self.tree, '#program-credits-text')[0].text.strip('\n'), expected)
        first = css(self.tree, '.program-credits-item')[0]
        self.assertEqual(css(first, 'button.copy-value-button')[0].get('data-copy-value'),
                         expected.split('\n\n')[0])
        self.assertEqual(text_of(css(first, '.program-credits-authors')[0]), 'Jan Kowalski, A. N. Pióro')
        self.assertEqual(self.tree.xpath('//button[@data-copy-target="#program-credits-text"]/text()'),
                         ['Kopiuj wszystko'])

    def test_unknown_anthology_is_explained(self):
        """Nieistniejąca antologia lub zły parametr – komunikat zamiast stopek."""
        for value in ('999999', 'abc'):
            with self.subTest(value=value):
                response = self.client.get(reverse('core:programs'), {'antologia': value})
                self.assertContains(response, 'Nie znaleziono wybranej antologii')


class ProgramJobPageTests(IsolatedProgramDirsMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('stan-zadania', 'Redaktor')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.member)
        with patch.object(jobs, 'launch'):
            response = self.client.post(reverse('core:programs'), {'program_action': 'clean', 'document': upload()})
        self.url = response['Location']
        self.folder = next(path for path in jobs.root().iterdir() if path.is_dir())

    def page(self, **state):
        jobs.write_json(self.folder / 'state.json', {'state': 'done', 'filename': 'tekst_odkurzony.docx',
                                                      'mime': 'application/octet-stream', 'warnings': [], **state})
        response = self.client.get(self.url)
        return response, html.fromstring(response.content.decode())

    def test_removed_images_message_with_polish_plurals(self):
        for count, word in ((1, 'obraz'), (3, 'obrazy'), (5, 'obrazów'), (12, 'obrazów'), (22, 'obrazy'),
                            (114, 'obrazów'), (101, 'obrazów')):
            with self.subTest(count=count):
                _, tree = self.page(removed_images=count)
                message = css(tree, '.program-images')
                self.assertEqual(len(message), 1)
                self.assertEqual(text_of(message[0]), f'Usunięto {count} {word} z dokumentu – wynik ich nie zawiera.')

    def test_no_message_without_images(self):
        for state in ({}, {'removed_images': 0}, {'removed_images': None}):
            with self.subTest(state=state):
                response, tree = self.page(**state)
                self.assertFalse(css(tree, '.program-images'))
                self.assertNotContains(response, 'Usunięto')

    def test_summary_table_and_dictionary_replacements(self):
        response, tree = self.page(
            summary=[{'rule': 'spaces', 'label': 'Podwójne i wielokrotne spacje', 'count': 5}],
            replacements=[{'source': 'pickupem', 'target': 'pikapem', 'count': 2},
                          {'source': '<b>', 'target': 'x', 'count': 1}])
        self.assertContains(response, '<td>Podwójne i wielokrotne spacje</td><td>5</td>', html=False)
        table = css(tree, '.program-summary table')[0]
        self.assertEqual(table.get('data-pagination'), 'off')
        items = [text_of(item) for item in css(tree, '.program-replacements li')]
        self.assertEqual(items, ['pickupem → pikapem (2)', '<b> → x (1)'])
        self.assertNotContains(response, '<b> →', html=False)

    def test_replacements_hidden_when_missing_or_damaged(self):
        for value in (None, [], 'zepsute', [{'source': 'a'}], [None]):
            with self.subTest(value=value):
                state = {'summary': []} if value is None else {'summary': [], 'replacements': value}
                _, tree = self.page(**state)
                self.assertFalse(css(tree, '.program-replacements li'))
