"""Nazwa pliku etapu w karcie tekstu (wzór z „Nazewnictwa plików”) z przyciskiem „Kopiuj”."""
from types import SimpleNamespace as NS

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.services.texts import withdraw_text
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from workflow.file_names import author_surnames, file_label, stage_file_name
from workflow.models import WorkflowStage
from workflow.services import resume_editing

StageType = WorkflowStage.StageType


def stage(kind, completed=False):
    return NS(stage_type=kind, is_completed=completed)


class FileLabelTests(SimpleTestCase):
    def test_label_for_each_stage(self):
        cases = {
            StageType.READY_FOR_EDITING: 'ORYGINAŁ',
            StageType.FIRST_VERIFICATION: 'REDAKCJA W1',
            StageType.SECOND_VERIFICATION: 'REDAKCJA W2',
            StageType.EDITING_CONTROL: '1 KOREKTA POCZ',
            StageType.FIRST_PROOFREADING: '2 KOREKTA POCZ',
            StageType.SECOND_PROOFREADING: 'W3 POCZ',
            StageType.THIRD_VERIFICATION: 'W3 ZAK',
            StageType.COORDINATOR_CONTROL: 'WERYFIKACJA R POCZ',
            StageType.EDITOR_CONTROL: 'WERYFIKACJA R ZAK',
            StageType.THIRD_PROOFREADING: '3 KOREKTA ZAK',
            StageType.FOURTH_PROOFREADING: 'DO STYLOWANIA',
            StageType.STYLING: 'GOTOWE',
            StageType.READY: 'GOTOWE',
            StageType.WITHDRAWN: None,
        }
        for kind, label in cases.items():
            with self.subTest(kind):
                self.assertEqual(file_label(stage(kind), []), label)
        self.assertIsNone(file_label(None, []))

    def test_editing_file_depends_on_finished_verifications(self):
        cases = {
            'before first verification': ([], 'REDAKCJA R POCZ'),
            'after first verification': ([StageType.FIRST_VERIFICATION], 'REDAKCJA R ZAK'),
            'after second verification': ([StageType.FIRST_VERIFICATION, StageType.SECOND_VERIFICATION], 'REDAKCJA ZAK'),
            'further editing after control': ([StageType.EDITING_CONTROL], 'REDAKCJA ZAK'),
        }
        for case, (done, label) in cases.items():
            for kind in (StageType.EDITING, StageType.AUTHOR_EDITING):
                with self.subTest(case, stage=kind):
                    self.assertEqual(file_label(stage(kind), [stage(item, True) for item in done]), label)

    def test_author_part_of_the_name(self):
        person = {'first_name': 'Jan', 'last_name': 'Kowalski', 'display_name': 'Jan Kowalski'}
        pen_name = {'first_name': 'Anna', 'last_name': 'Nowak', 'display_name': 'A.N. Wiedźma'}
        cases = {
            'surname': ([person], 'Kowalski'),
            'pseudonym instead of surname': ([pen_name], 'A.N. Wiedźma'),
            'several authors, no duplicates': ([person, pen_name, person], 'Kowalski, A.N. Wiedźma'),
            'no authors': ([], ''),
        }
        for case, (authors, expected) in cases.items():
            with self.subTest(case):
                self.assertEqual(author_surnames(authors), expected)
        self.assertEqual(stage_file_name('Latarnik', [person], stage(StageType.FIRST_VERIFICATION), []),
                         'REDAKCJA W1 Kowalski – Latarnik')
        self.assertEqual(stage_file_name('Latarnik', [], stage(StageType.READY_FOR_EDITING), []), 'ORYGINAŁ Latarnik')
        self.assertIsNone(stage_file_name('Latarnik', [person], stage(StageType.WITHDRAWN), []))


class StageFileOnTextPageTests(WorkflowTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.text.authors.add(Author.objects.create(first_name='Jan', last_name='Kowalski'))

    def box(self, user=None):
        self.client.force_login(user or self.editor)
        page = html.fromstring(self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk])).content)
        found = page.xpath('//*[@data-stage-file]')
        if not found:
            return None
        button = found[0].xpath('.//button[contains(@class, "copy-value-button")]')[0]
        label = found[0].xpath('string(.//*[@class="stage-file-label"])')
        return label, found[0].xpath('string(.//code)'), button.get('data-copy-value'), button.text_content().strip()

    def test_file_name_follows_the_current_stage(self):
        title = 'Tekst do testów workflow'
        steps = [
            ('ready for editing', lambda: None, 'Do redakcji', f'ORYGINAŁ Kowalski – {title}'),
            ('editing', self.begin_editing, 'Redakcja', f'REDAKCJA R POCZ Kowalski – {title}'),
        ]
        for case, action, stage_name, expected in steps:
            action()
            with self.subTest(case):
                self.assertEqual(self.box(), (f'{stage_name}. Plik nazwij w ten sposób:', expected, expected, 'Kopiuj'))

    def test_editing_after_first_verification(self):
        self.finish_first_verification()
        resume_editing(self.text, self.editor, started_at=self.today)
        self.assertEqual(self.box()[1], 'REDAKCJA R ZAK Kowalski – Tekst do testów workflow')

    def test_withdrawn_text_has_no_file_name(self):
        withdraw_text(user=self.coordinator, text_id=self.text.pk)
        self.assertIsNone(self.box(self.coordinator))
