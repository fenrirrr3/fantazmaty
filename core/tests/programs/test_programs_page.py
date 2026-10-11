"""Wygląd stron programów: zwarte karty, dymki „?” z opisami i strona stanu zadania."""
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
    def test_superuser_limits_are_in_tips(self):
        admin = create_superuser('wyglad-admin')
        self.client.force_login(admin)
        tree = html.fromstring(self.client.get(reverse('core:programs')).content.decode())
        tips = ' '.join(text_of(body) for body in css(tree, '.help-tip-body'))
        self.assertIn('nie masz limitu wielkości analizy', tips)
        self.assertIn(f'do {jobs.superuser_time_limit()} sekund', tips)


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
