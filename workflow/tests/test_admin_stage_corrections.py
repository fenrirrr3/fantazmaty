"""Poprawki etapów i rekordów w adminie: cofnięcie zakończenia, usuwanie etapu, ochrona tekstów i przypisań z historią."""
from datetime import timedelta

from django.contrib.admin.sites import site
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from core.selectors.texts import _annotated_texts, text_detail_context
from fantazmaty.testing import create_member, create_superuser, form_value
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from texts.models import Anthology, Review, Text
from workflow.admin_assignment_edit import correct_assignment
from workflow.admin_stage_edit import edit_stage
from workflow.import_context import importing_completed
from workflow.handoffs import handoff_stage
from workflow.models import WorkflowRepetition, WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.repetitions import repeat_stages
from workflow.services import claim_stage, complete_stage
from workflow.state import current_stage

from .support import importing, work_state


class AdminCorrectionStartTests(TestCase):
    """Wspólny początek korekt w adminie (``admin_correction``) w każdej operacji: tylko superuser, aktualna
    wersja formularza, antologia w przygotowaniu (poprawka wykonawców sprawdza antologię przy zmianie);
    odmowa niczego nie zmienia."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Korektor')
        cls.other = create_member('other', 'Korektor')
        cls.book = Anthology.objects.create(title='Test')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.assignment = A.objects.create(text=cls.text, role='proofreader_1', assigned_to=cls.member)
        cls.stage = S.objects.create(text=cls.text, assignment=cls.assignment, stage_type='first_proofreading',
                                     started_at=cls.today - timedelta(days=10), ended_at=cls.today, is_completed=True)

    def operations(self):
        from workflow.admin_add_stage import add_missing_stage
        from workflow.admin_performers import correct_stage_performers
        from workflow.admin_stage_dates import set_stage_dates
        from workflow.admin_status import set_admin_status
        return {
            'status': lambda actor, version: set_admin_status(self.text.pk, 'editing_control', actor, version),
            'dodanie etapu': lambda actor, version: add_missing_stage(self.text.pk, actor, version,
                                                                      kind='second_proofreading'),
            'dodatkowe wykonanie': lambda actor, version: add_missing_stage(
                self.text.pk, actor, version, kind='first_proofreading', performer=self.other, additional=True),
            'przypisanie': lambda actor, version: correct_assignment(self.assignment.pk, actor, version,
                                                                     action='clear'),
            'wykonawcy': lambda actor, version: correct_stage_performers(self.text.pk, {self.stage.pk: self.other},
                                                                         actor, version),
            'daty': lambda actor, version: set_stage_dates(self.stage.pk, actor, version,
                                                           started_at=self.stage.started_at, ended_at=self.today),
            'etap': lambda actor, version: edit_stage(self.stage.pk, actor, version, action='reopen'),
        }

    @staticmethod
    def state():
        return list(S.objects.order_by('pk').values()), list(A.objects.order_by('pk').values())

    def test_every_admin_correction_checks_actor_version_and_anthology(self):
        version = version_of(self.text)
        before = self.state()
        for name, operation in self.operations().items():
            for case, actor, form_version, error, message in (
                    ('bez uprawnień', self.member, version, PermissionDenied, ''),
                    ('nieaktywny superuser', type(self.admin)(pk=self.admin.pk, is_superuser=True, is_active=False),
                     version, PermissionDenied, ''),
                    ('nieaktualny formularz', self.admin, version - 1, ValidationError, 'Dane zmieniły się'),
                    ('gotowa antologia', self.admin, version, ValidationError, 'Antologia jest gotowa')):
                with self.subTest(name, case=case), transaction.atomic():
                    if case == 'gotowa antologia':
                        Anthology.objects.filter(pk=self.book.pk).update(status=Anthology.Status.READY)
                    with self.assertRaisesMessage(error, message):
                        operation(actor, form_version)
                    self.assertEqual(self.state(), before)
                    transaction.set_rollback(True)
        self.assertEqual(version_of(self.text), version)


class ReopenStageTests(TestCase):
    """Cofnięcie zakończenia etapu przez superusera: ta sama osoba, wykonanie i data rozpoczęcia."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Korektor')
        cls.book = Anthology.objects.create(title='Test')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.assignment = A.objects.create(text=cls.text, role='proofreader_1', assigned_to=cls.member)
        cls.stage = S.objects.create(text=cls.text, assignment=cls.assignment, stage_type='first_proofreading',
                                     started_at=cls.today - timedelta(days=10), ended_at=cls.today, is_completed=True)

    def setUp(self):
        self.client.force_login(self.admin)

    def reopen(self, actor=None, version=None):
        return edit_stage(self.stage.pk, actor or self.admin,
                          version_of(self.text) if version is None else version, action='reopen')

    def test_reopen_keeps_start_performer_execution_and_becomes_active(self):
        old_version = version_of(self.text)
        original_start = self.stage.started_at
        self.reopen()
        self.stage.refresh_from_db()
        self.assertIsNone(self.stage.ended_at)
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(self.stage.started_at, original_start)
        self.assertEqual(self.stage.assignment_id, self.assignment.pk)
        self.assertEqual(self.stage.execution_number, 1)
        self.assertEqual(S.objects.filter(text=self.text).count(), 1)
        self.assertGreater(version_of(self.text), old_version)
        row = work_state(self.member, self.text)
        self.assertTrue(row.work_active)
        self.assertFalse(row.work_completed)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, self.stage.stage_type)

    def test_empty_continuation_is_retired_and_completion_can_be_saved_again(self):
        pending = S.objects.create(text=self.text, stage_type='second_proofreading')
        self.reopen()
        pending.refresh_from_db()
        self.assertFalse(pending.is_current or pending.is_released)
        self.assertEqual(current_stage(list(self.text.workflow_stages.all())).pk, self.stage.pk)
        self.stage.refresh_from_db()
        complete_stage(self.stage, self.member, self.today)
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(self.stage.ended_at, self.today)
        self.assertEqual(S.objects.current_cycle().filter(text=self.text, stage_type='second_proofreading').count(), 1)

    def test_later_started_reserved_or_completed_work_blocks_and_preserves_both_rows(self):
        for state in ('started', 'reserved', 'completed'):
            with self.subTest(state=state):
                assignment = A.objects.create(text=self.text, role='proofreader_2', assigned_to=self.member if state == 'reserved' else None)
                later = S.objects.create(text=self.text, stage_type='second_proofreading', assignment=assignment,
                                         started_at=self.today if state != 'reserved' else None,
                                         ended_at=self.today if state == 'completed' else None, is_completed=state == 'completed')
                with self.assertRaisesMessage(ValidationError, 'Dalsza praca'):
                    self.reopen()
                self.stage.refresh_from_db()
                later.refresh_from_db()
                self.assertTrue(self.stage.is_completed)
                self.assertTrue(later.is_current)
                later.delete()
                assignment.delete()

    def test_admin_inline_link_and_post_reopen(self):
        change_url = reverse('admin:texts_text_change', args=[self.text.pk])
        response = self.client.get(change_url)
        self.assertContains(response, '?action=reopen')
        self.assertContains(response, 'Cofnij zakończenie')
        url = reverse('admin:workflow_stage_correct', args=[self.stage.pk])
        response = self.client.get(url, {'action': 'reopen'})
        self.assertEqual(response.context['form'].initial['action'], 'reopen')
        response = self.client.post(url, {'action': 'reopen', 'version': version_of(self.text), 'confirm': 'on'})
        self.assertRedirects(response, change_url, fetch_redirect_response=False)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)
        response = self.client.get(change_url)
        self.assertNotContains(response, '?action=reopen')

    def test_imported_stage_with_start_loses_completed_import_exception(self):
        with importing():
            self.stage.imported_completed = True
            self.stage.save()
        start = self.stage.started_at
        self.reopen()
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.imported_completed)
        self.assertFalse(importing_completed.get())
        self.assertEqual(self.stage.started_at, start)
        self.stage.full_clean()

    def test_imported_stage_without_start_shows_form_error_without_guessing_date(self):
        with importing():
            self.stage.imported_completed = True
            self.stage.started_at = None
            self.stage.ended_at = None
            self.stage.save()
        response = self.client.post(reverse('admin:workflow_stage_correct', args=[self.stage.pk]),
                                    {'action': 'reopen', 'version': version_of(self.text), 'confirm': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Najpierw uzupełnij ją')
        self.stage.refresh_from_db()
        self.assertTrue(self.stage.is_completed and self.stage.imported_completed)
        self.assertIsNone(self.stage.started_at)

    def test_archived_execution_and_withdrawn_text_cannot_reopen(self):
        self.stage.is_current = False
        self.stage.save()
        with self.assertRaises(ValidationError):
            self.reopen()
        self.stage.is_current = True
        self.stage.save()
        S.objects.create(text=self.text, stage_type='withdrawn')
        with self.assertRaisesMessage(ValidationError, 'Tekst jest wycofany'):
            self.reopen()

    def test_only_genuinely_performed_current_work_can_be_reopened(self):
        def canceled_queue():
            run = WorkflowRepetition.objects.create(text=self.text, canceled_at=timezone.now())
            S.objects.filter(pk=self.stage.pk).update(repetition=run)

        def other_open_queue():
            WorkflowRepetition.objects.create(text=self.text)

        cases = (
            (canceled_queue, 'odwołanej kolejki'),
            (lambda: S.objects.filter(pk=self.stage.pk).update(is_skipped=True, stage_type='fourth_proofreading',
                                                               assignment=None, started_at=None,
                                                               ended_at=None), 'pominięty'),
            (lambda: S.objects.filter(pk=self.stage.pk).update(started_at=self.today + timedelta(days=1),
                                                               ended_at=self.today + timedelta(days=1)),
             'przyszłej daty rozpoczęcia'),
            (lambda: A.objects.filter(pk=self.assignment.pk).update(is_current=False), 'zastąpionego wykonawcę'),
            (other_open_queue, 'kolejkę powtórzeń'),
        )
        for prepare, message in cases:
            with self.subTest(message=message), transaction.atomic():
                savepoint = transaction.savepoint()
                prepare()
                with self.assertRaisesMessage(ValidationError, message):
                    self.reopen()
                self.assertTrue(S.objects.get(pk=self.stage.pk).is_completed)
                transaction.savepoint_rollback(savepoint)

    def test_ready_marker_is_retired_when_last_work_is_reopened(self):
        assignment = A.objects.create(text=self.text, role='styling', assigned_to=self.admin)
        self.stage.stage_type = 'styling'
        self.stage.assignment = assignment
        self.stage.save()
        marker = S.objects.create(text=self.text, stage_type='ready', started_at=self.today)
        self.reopen()
        marker.refresh_from_db()
        self.assertFalse(marker.is_current)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, 'styling')

    def test_last_stage_of_completed_repeat_can_reopen_and_finish_same_execution(self):
        S.objects.create(text=self.text, stage_type='ready')
        # A later, unselected completed checkpoint must remain unchanged.
        later = S.objects.create(text=self.text, stage_type='third_proofreading', started_at=self.today,
                                 ended_at=self.today, is_completed=True)
        run = repeat_stages(self.text, ['first_proofreading'], self.admin)
        self.stage = claim_stage(self.text, 'first_proofreading', self.member)
        complete_stage(self.stage, self.member, self.today)
        original_pk = self.stage.pk
        original_execution = self.stage.execution_number
        self.reopen()
        run.refresh_from_db()
        later.refresh_from_db()
        self.assertIsNone(run.completed_at)
        self.assertTrue(later.is_current and later.is_completed)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)
        self.assertEqual(self.stage.pk, original_pk)
        self.assertEqual(self.stage.execution_number, original_execution)
        complete_stage(self.stage, self.member, self.today)
        run.refresh_from_db()
        self.assertIsNotNone(run.completed_at)
        self.assertEqual(run.stages.count(), 1)


class AdditionalExecutionTests(TestCase):
    """„Dodaj brakujący etap” z opcją „Dodatkowe wykonanie istniejącego etapu”: zakończona praca kolejnej osoby
    jako wyk. 2, 3… bez powtórzenia workflow, bez zmiany statusu, bieżącego wykonania i obciążenia."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Korektor')
        cls.other = create_member('other', 'Korektor')
        cls.book = Anthology.objects.create(title='Test')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.first = S.objects.create(
            text=cls.text, stage_type='first_proofreading', is_completed=True,
            started_at=cls.today - timedelta(days=10), ended_at=cls.today - timedelta(days=5),
            assignment=A.objects.create(text=cls.text, role='proofreader_1', assigned_to=cls.member))
        cls.second = create_member('second', 'Korektor')
        cls.open = S.objects.create(
            text=cls.text, stage_type='second_proofreading', started_at=cls.today - timedelta(days=2),
            assignment=A.objects.create(text=cls.text, role='proofreader_2', assigned_to=cls.second))

    def add(self, kind='first_proofreading', performer=None, **data):
        from workflow.admin_add_stage import add_missing_stage
        self.text.refresh_from_db()
        return add_missing_stage(self.text.pk, self.admin, version_of(self.text), kind=kind,
                                 performer=performer or self.other, additional=True, **data)

    @staticmethod
    def state():
        return (list(S.objects.filter(is_current=True).order_by('pk').values()),
                list(A.objects.filter(is_current=True).order_by('pk').values()))

    def test_completed_and_open_stage_get_next_execution_without_touching_workflow(self):
        from workflow.labels import assignment_label, stage_label
        before, status = self.state(), current_stage(list(self.text.workflow_stages.all())).pk
        start, end = self.today - timedelta(days=4), self.today - timedelta(days=3)
        cases = [
            ('zakończony etap, inna osoba, z datami', 'first_proofreading', self.other,
             {'started_at': start, 'ended_at': end}, 2, (start, end, False)),
            ('ta sama osoba jeszcze raz, bez dat', 'first_proofreading', self.member, {}, 3, (None, None, True)),
            ('sama data zakończenia', 'first_proofreading', self.other, {'ended_at': end}, 4, (None, end, True)),
            ('etap otwarty (bieżący)', 'second_proofreading', self.other, {}, 2, (None, None, True)),
        ]
        for case, kind, performer, data, number, (started, ended, imported) in cases:
            with self.subTest(case):
                stage = self.add(kind, performer, **data)
                self.assertEqual((stage.execution_number, stage.is_current, stage.is_released, stage.is_completed),
                                 (number, False, False, True))
                self.assertEqual((stage.started_at, stage.ended_at, stage.imported_completed), (started, ended, imported))
                assignment = stage.assignment
                self.assertEqual((assignment.assigned_to, assignment.execution_number, assignment.is_current,
                                  assignment.assigned_at), (performer, number, False, None))
                label = dict(S.StageType.choices)[kind]
                self.assertEqual(stage_label(stage), f'{label} (wyk. {number})')
                self.assertIn(f'(wyk. {number})', assignment_label(assignment))
        self.assertEqual(self.state(), before)
        self.assertEqual(current_stage(list(self.text.workflow_stages.all())).pk, status)
        self.open.refresh_from_db()
        self.assertFalse(self.open.is_completed)
        # „Moje teksty”: dopisana osoba ma pracę zakończoną, nie aktywną; prowadzący nadal pracuje.
        other, second = work_state(self.other, self.text), work_state(self.second, self.text)
        self.assertEqual((other.work_active, other.work_completed), (False, True))
        self.assertTrue(second.work_active)

    def test_number_counts_performed_work_not_assignment_rows(self):
        """Jedna wykonana W1 po przekazaniu pracy (dwa przydziały roli) albo z wycofaną rezerwacją – dodatkowa W1
        to „wyk. 2”; numer przydziału pozostaje osobny i unikalny dla roli."""
        verifier, successor = create_member('verifier', 'Weryfikator'), create_member('successor', 'Weryfikator')
        A.objects.create(text=self.text, role='verifier_1', assigned_to=verifier, is_current=False, execution_number=1)
        handed = A.objects.create(text=self.text, role='verifier_1', assigned_to=successor, execution_number=2)
        S.objects.create(text=self.text, stage_type='first_verification', is_completed=True, assignment=handed,
                         started_at=self.today - timedelta(days=9), ended_at=self.today - timedelta(days=8))
        S.objects.create(text=self.text, stage_type='first_verification', iteration=2, execution_number=2,
                         is_current=False, is_released=False)  # wycofana rezerwacja
        stage = self.add('first_verification', self.other)
        self.assertEqual((stage.execution_number, stage.assignment.execution_number), (2, 3))
        from workflow.labels import assignment_label, stage_label
        self.assertEqual(stage_label(stage), 'Pierwsza weryfikacja (wyk. 2)')
        self.assertTrue(assignment_label(stage.assignment).endswith('(wyk. 2)'))
        self.assertEqual(self.add('first_verification', self.member).execution_number, 3)

    def test_extra_execution_is_a_team_tile_on_text_page(self):
        """Strona tekstu (moje-teksty/<numer>): dopisana osoba jest w kafelkach zespołu zaraz po swojej roli,
        a nie w „Historii przypisań osób”."""
        stage = self.add()
        context = text_detail_context(user=self.admin, text=self.text)
        tiles = [(member['role'], member['label'], member['user']['pk'] if member['user'] else None)
                 for member in context['team_members']]
        role_label = dict(A.Role.choices)['proofreader_1']
        main = tiles.index(('proofreader_1', role_label, self.member.pk))
        self.assertEqual(tiles[main + 1], ('proofreader_1', f'{role_label} (wyk. 2)', self.other.pk))
        self.assertNotIn(stage.assignment.assigned_to.pk,
                         [member['user']['pk'] for member in context['previous_team_members'] if member['user']])
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertContains(page, f'{role_label} (wyk. 2)')

    def test_completed_verification_does_not_require_repetition(self):
        """Zakończona W1 zwykle wymaga powtórzenia etapów – dodatkowe wykonanie nie."""
        verifier = create_member('verifier', 'Weryfikator')
        S.objects.create(text=self.text, stage_type='first_verification', is_completed=True,
                         started_at=self.today - timedelta(days=9), ended_at=self.today - timedelta(days=8),
                         assignment=A.objects.create(text=self.text, role='verifier_1', assigned_to=verifier))
        stage = self.add('first_verification', self.other)
        self.assertEqual((stage.execution_number, stage.assignment.role), (2, 'verifier_1'))

    def test_invalid_requests_change_nothing(self):
        before = list(S.objects.order_by('pk').values()), list(A.objects.order_by('pk').values())
        end = self.today - timedelta(days=3)
        for case, kwargs, message in (
                ('bez wykonawcy', {'performer': None}, 'wymaga wykonawcy'),
                ('etapu nie ma w przebiegu', {'kind': 'third_proofreading'}, 'Tego etapu nie ma jeszcze'),
                ('tylko data rozpoczęcia', {'started_at': end}, 'podaj też datę zakończenia'),
                ('zakończenie przed rozpoczęciem', {'started_at': end, 'ended_at': end - timedelta(days=1)},
                 'nie może być wcześniejsza'),
                ('razem z pracą historyczną', {'historical': True}, 'Wybierz jeden tryb'),
                ('K1 dla osoby z otwartą K2', {'performer': self.second}, 'ma otwartą drugą lub trzecią korektę'),
                ('etap spoza listy', {'kind': 'ready'}, 'Wybierz etap pracy'),
        ):
            with self.subTest(case):
                kind = kwargs.pop('kind', 'first_proofreading')
                performer = kwargs.pop('performer', self.other)
                from workflow.admin_add_stage import add_missing_stage
                with self.assertRaisesMessage(ValidationError, message):
                    add_missing_stage(self.text.pk, self.admin, version_of(self.text), kind=kind,
                                      performer=performer, additional=True, **kwargs)
        from workflow.admin_add_stage import add_missing_stage
        with self.assertRaisesMessage(ValidationError, 'Nieprawidłowy tryb dodatkowego wykonania'):
            add_missing_stage(self.text.pk, self.admin, version_of(self.text), kind='first_proofreading',
                              performer=self.other, additional='on')
        self.assertEqual((list(S.objects.order_by('pk').values()), list(A.objects.order_by('pk').values())), before)

    def test_extra_execution_is_corrected_with_existing_admin_tools(self):
        """Zapisane dodatkowe wykonanie: korekta dat i wykonawcy jak w innych zakończonych etapach, „Cofnij
        zakończenie” niedostępne (to nie bieżące wykonanie), usunięcie zabiera też jego przydział."""
        from workflow.admin_stage_dates import set_stage_dates
        stage = self.add()
        start, end = self.today - timedelta(days=4), self.today - timedelta(days=3)
        set_stage_dates(stage.pk, self.admin, version_of(self.text), started_at=start, ended_at=end)
        stage.refresh_from_db()
        self.assertEqual((stage.started_at, stage.ended_at, stage.is_current), (start, end, False))
        third = create_member('third', 'Korektor')
        edit_stage(stage.pk, self.admin, version_of(self.text), action='performer', performer=third)
        stage.refresh_from_db()
        self.assertEqual((stage.assignment.assigned_to, stage.execution_number), (third, 2))
        with self.assertRaisesMessage(ValidationError, 'tylko aktualnego, zakończonego etapu'):
            edit_stage(stage.pk, self.admin, version_of(self.text), action='reopen')
        assignment = stage.assignment_id
        edit_stage(stage.pk, self.admin, version_of(self.text), action='delete')
        self.assertFalse(S.objects.filter(pk=stage.pk).exists())
        self.assertFalse(A.objects.filter(pk=assignment).exists())
        self.first.refresh_from_db()
        self.assertTrue(self.first.is_current and self.first.is_completed)
        self.assertEqual(current_stage(list(self.text.workflow_stages.all())).pk, self.open.pk)

    def test_performer_correction_never_merges_main_and_extra_execution(self):
        """Korekta wykonawcy głównego etapu nie wchłania dodatkowego wykonania (i odwrotnie) – scalanie
        zdublowanych przydziałów roli pomija dodatkowe wykonania."""
        from workflow.admin_performers import correct_stage_performers
        extra = self.add()
        third = create_member('third', 'Korektor')
        correct_stage_performers(self.text.pk, {self.first.pk: third}, self.admin, version_of(self.text))
        self.first.refresh_from_db()
        extra.refresh_from_db()
        self.assertEqual((self.first.assignment.assigned_to, self.first.assignment.is_current), (third, True))
        self.assertEqual((extra.assignment.assigned_to, extra.assignment.is_current, extra.assignment.is_additional),
                         (self.other, False, True))
        self.assertNotEqual(self.first.assignment_id, extra.assignment_id)
        correct_stage_performers(self.text.pk, {extra.pk: self.member}, self.admin, version_of(self.text))
        self.first.refresh_from_db()
        extra.refresh_from_db()
        self.assertEqual((self.first.assignment.assigned_to, extra.assignment.assigned_to), (third, self.member))
        self.assertEqual(A.objects.filter(text=self.text, role='proofreader_1').count(), 2)

    def test_text_change_form_keeps_and_corrects_both_rows(self):
        """Zapis strony tekstu w adminie z tabelą wykonawców: bez zmian – oba wykonania zostają; zmiana osoby
        w wierszu „wyk. 2” zmienia tylko dodatkowe wykonanie."""
        from authors.models import Author
        from core.tests.people.support import admin_form_data
        self.text.authors.add(Author.objects.create(first_name='Jan', last_name='Autor'))
        extra = self.add()
        self.client.force_login(self.admin)
        url = reverse('admin:texts_text_change', args=[self.text.pk])
        data = admin_form_data(self.client, url, 'text_form')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        third = create_member('third', 'Korektor')
        rows = [key for key, value in data.items() if key.endswith('-id') and value == str(extra.pk)]
        self.assertEqual(len(rows), 1)
        data = admin_form_data(self.client, url, 'text_form', **{rows[0].replace('-id', '-performer'): str(third.pk)})
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.first.refresh_from_db()
        extra.refresh_from_db()
        self.assertEqual((self.first.assignment.assigned_to, extra.assignment.assigned_to), (self.member, third))
        self.assertEqual(A.objects.filter(text=self.text, role='proofreader_1').count(), 2)

    def test_later_repetition_continues_numbering(self):
        """Powtórzenie etapu po dodatkowym wykonaniu dostaje kolejny numer (wyk. 3), dodatkowe zostaje w historii."""
        extra = self.add()
        S.objects.filter(pk=self.open.pk).update(is_completed=True, ended_at=self.today)
        self.text.refresh_from_db()
        repeat_stages(self.text, ['first_proofreading'], self.admin)
        repeated = S.objects.get(text=self.text, stage_type='first_proofreading', is_current=True)
        self.assertEqual((repeated.execution_number, repeated.assignment.execution_number), (3, 3))
        extra.refresh_from_db()
        self.assertEqual((extra.execution_number, extra.is_current, extra.is_completed), (2, False, True))

    def test_stage_only_in_previous_cycle_and_former_member(self):
        """Etap z poprzedniego przebiegu to nie „istniejący etap”; dawny (nieaktywny) członek zespołu może zostać
        dopisany jak przy pracy historycznej."""
        former = create_member('former', 'Korektor')
        type(former).objects.filter(pk=former.pk).update(is_active=False)
        former.refresh_from_db()
        self.assertEqual(self.add(performer=former).assignment.assigned_to, former)
        Text.objects.filter(pk=self.text.pk).update(current_workflow_cycle=2)
        with self.assertRaisesMessage(ValidationError, 'Tego etapu nie ma jeszcze w bieżącym przebiegu'):
            self.add()

    def test_extra_execution_is_visible_in_text_history_and_summary(self):
        """Strona tekstu (historia dla koordynatora) i tabelka zbiorcza pokazują dopisaną osobę."""
        stage = self.add()
        detail = text_detail_context(user=self.admin, text=self.text)
        archived = {row['pk'] for row in detail['archived_stages']}
        self.assertIn(stage.pk, archived)
        self.assertNotIn(stage.pk, {row['pk'] for row in detail['visible_stages']})
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:workflow_list'))
        self.assertContains(page, self.other.get_full_name())

    def test_correction_forms_put_checkbox_before_label_with_help_below(self):
        """Formularze korekt (brakujący etap, status, daty, korekta etapu i przypisania): każde pole wyboru jest
        w podpisie przed jego tekstem, a opis – osobno pod spodem; pola i błędy formularza zostają."""
        from lxml import html
        self.client.force_login(self.admin)
        pages = {
            'brakujący etap': reverse('admin:texts_text_add_stage', args=[self.text.pk]),
            'status': reverse('admin:texts_text_manual_status', args=[self.text.pk]),
            'daty': reverse('admin:workflow_stage_dates', args=[self.open.pk]),
            'korekta etapu': reverse('admin:workflow_stage_correct', args=[self.first.pk]),
            'korekta przypisania': reverse('admin:workflow_assignment_correct', args=[self.first.assignment_id]),
        }
        for name, url in pages.items():
            with self.subTest(name):
                tree = html.fromstring(self.client.get(url).content.decode())
                boxes = tree.xpath('//form//input[@type="checkbox"]')
                for box in boxes:
                    label = box.getparent()
                    self.assertEqual((label.tag, label.get('for')), ('label', box.get('id')))
                    self.assertIn('cms-admin-check', label.getparent().get('class'))
                    self.assertTrue(label.xpath('./span/text()'))
                self.assertTrue(tree.xpath('//form//input[@name="version"][@type="hidden"]'))
        tree = html.fromstring(self.client.get(pages['brakujący etap']).content.decode())
        help_text = tree.xpath('//input[@name="additional"]/ancestor::div[1]/p[@class="help"]/text()')
        self.assertIn('bez powtórzenia, cofania i zmiany statusu', help_text[0])
        error = self.client.post(pages['status'], {'stage': 'editing', 'version': version_of(self.text)})
        self.assertContains(error, 'To pole jest wymagane.')

    def test_admin_form_saves_and_inline_shows_execution(self):
        self.client.force_login(self.admin)
        url = reverse('admin:texts_text_add_stage', args=[self.text.pk])
        page = self.client.get(url)
        self.assertContains(page, 'Dodatkowe wykonanie istniejącego etapu (wyk. 2, 3…)')
        self.assertContains(page, 'name="additional"')
        response = self.client.post(url, {'kind': 'first_proofreading', 'performer': self.other.pk, 'additional': 'on',
                                          'version': version_of(self.text)}, follow=True)
        self.assertRedirects(response, reverse('admin:texts_text_change', args=[self.text.pk]))
        self.assertContains(response, 'Zapisano etap i wykonawcę: Pierwsza korekta (wyk. 2).')
        self.assertContains(response, 'Pierwsza korekta (wyk. 2)')
        error = self.client.post(url, {'kind': 'third_proofreading', 'performer': self.other.pk, 'additional': 'on',
                                       'version': version_of(self.text)})
        self.assertContains(error, 'Tego etapu nie ma jeszcze w bieżącym przebiegu')


class StageDeletionTests(TestCase):
    """Usunięcie etapu w adminie: bez zastępstwa bieżącym staje się ostatni zakończony etap."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.member = create_member('reader', 'Recenzent')
        cls.book = Anthology.objects.create(title='Test')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.today = timezone.localdate()

    def setUp(self):
        self.client.force_login(self.admin)

    def completed(self, kind='editing', **kwargs):
        return S.objects.create(text=self.text, stage_type=kind, is_completed=True,
                                started_at=self.today, ended_at=self.today, **kwargs)

    def test_delete_without_replacement_keeps_completed_last_stage(self):
        last = self.completed('third_proofreading')
        self.completed('editing')  # PK/date must not override workflow order.
        assignment = A.objects.create(text=self.text, role='proofreader_4', assigned_to=self.member)
        removed = S.objects.create(text=self.text, stage_type='fourth_proofreading', assignment=assignment)
        edit_stage(removed.pk, self.admin, version_of(self.text), action='delete')
        self.assertFalse(S.objects.filter(pk=removed.pk).exists())
        self.assertFalse(A.objects.filter(pk=assignment.pk).exists())
        self.assertEqual(current_stage(list(self.text.workflow_stages.all())).pk, last.pk)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, last.stage_type)
        last.refresh_from_db()
        self.assertTrue(last.is_completed)
        self.assertEqual(last.ended_at, self.today)
        self.assertEqual(S.objects.filter(text=self.text).count(), 2)
        detail = text_detail_context(user=self.admin, text=self.text)
        self.assertEqual(len(detail['visible_stages']), len({row['pk'] for row in detail['visible_stages']}))

    def test_admin_delete_form_accepts_empty_replacement(self):
        self.completed()
        removed = S.objects.create(text=self.text, stage_type='first_proofreading')
        url = reverse('admin:workflow_stage_correct', args=[removed.pk])
        response = self.client.post(url, {'action': 'delete', 'replacement': '',
                                          'version': version_of(self.text), 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(S.objects.filter(pk=removed.pk).exists())

    def test_deleting_last_record_leaves_empty_workflow(self):
        removed = S.objects.create(text=self.text, stage_type='ready_for_editing')
        edit_stage(removed.pk, self.admin, version_of(self.text), action='delete')
        self.assertFalse(self.text.workflow_stages.exists())
        self.assertIsNone(_annotated_texts().get(pk=self.text.pk).current_stage_type)

    def test_explicit_replacement_still_opens_chosen_stage(self):
        self.completed()
        removed = S.objects.create(text=self.text, stage_type='first_proofreading')
        edit_stage(removed.pk, self.admin, version_of(self.text), action='delete', replacement='second_proofreading')
        stage = current_stage(list(self.text.workflow_stages.all()))
        self.assertEqual(stage.stage_type, 'second_proofreading')
        self.assertFalse(stage.is_completed)
        self.assertIsNone(stage.started_at)

    def test_current_stage_ignores_archived_and_unreleased_work(self):
        last = self.completed()
        self.completed('third_proofreading', is_current=False)
        S.objects.create(text=self.text, stage_type='fourth_proofreading', is_released=False)
        self.assertEqual(current_stage(list(self.text.workflow_stages.all())).pk, last.pk)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, 'editing')



class StageCorrectionFormTests(TestCase):
    """Formularz „Popraw wykonanie etapu”: zmiana wykonawcy w miejscu, ochrona historii powtórzeń i przekazań."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.member = create_member('member', 'Redaktor')
        cls.other = create_member('other', 'Redaktor')
        cls.today = timezone.localdate()

    def setUp(self):
        self.text = Text.objects.create(title='Tekst', length=100)
        self.assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.stage = S.objects.create(text=self.text, stage_type='editing', assignment=self.assignment,
                                      started_at=self.today)
        self.url = reverse('admin:workflow_stage_correct', args=[self.stage.pk])
        self.client.force_login(self.admin)

    def test_performer_correction_keeps_assignment_and_requires_a_person(self):
        with self.assertRaisesMessage(ValidationError, 'Wybierz wykonawcę'):
            edit_stage(self.stage.pk, self.admin, version_of(self.text), action='performer')
        edit_stage(self.stage.pk, self.admin, version_of(self.text), action='performer', performer=self.other)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.assignment_id, self.assignment.pk)
        self.assertEqual(self.stage.assignment.assigned_to, self.other)
        self.assertEqual(self.stage.started_at, self.today)

    def test_open_repetition_queue_blocks_every_correction(self):
        S.objects.create(text=self.text, stage_type='ready')
        S.objects.filter(pk=self.stage.pk).update(ended_at=self.today, is_completed=True)
        run = repeat_stages(self.text, ['editing'], self.admin)
        queued = run.stages.get()
        for action in ('performer', 'reopen', 'delete', 'restore_reservation'):
            with self.subTest(action=action), self.assertRaisesMessage(ValidationError, 'kolejkę powtórzeń'):
                edit_stage(queued.pk, self.admin, version_of(self.text), action=action, performer=self.other)

    def test_snapshot_stage_and_handed_off_assignment_survive_deletion(self):
        run = WorkflowRepetition.objects.create(text=self.text, canceled_at=timezone.now(),
                                                previous_stage_ids=[self.stage.pk])
        with self.assertRaisesMessage(ValidationError, 'historii powtórzenia'):
            edit_stage(self.stage.pk, self.admin, version_of(self.text), action='delete')
        run.delete()
        # A stage with a recorded handoff is protected by the database and the form says so.
        handoff_stage(self.text, self.admin, stage_id=self.stage.pk, assigned_to_id=self.other.pk,
                      expected_assignment_id=self.assignment.pk)
        response = self.client.post(self.url, {'action': 'delete', 'version': version_of(self.text),
                                               'confirm': 'on'})
        self.assertContains(response, 'Etap ma powiązane przekazania pracy')
        self.assertTrue(S.objects.filter(pk=self.stage.pk).exists())
        # Deleting the last stage of a handoff participant keeps that assignment.
        control = S.objects.create(text=self.text, stage_type='editing_control', is_current=False,
                                   assignment=self.assignment, is_released=False)
        edit_stage(control.pk, self.admin, version_of(self.text), action='delete')
        self.assertFalse(S.objects.filter(pk=control.pk).exists())
        self.assertTrue(A.objects.filter(pk=self.assignment.pk).exists())

    def test_form_defaults_to_performer_and_shows_service_errors(self):
        response = self.client.get(self.url, {'action': 'drop_table'})
        self.assertEqual(response.context['form'].initial['action'], 'performer')
        self.assertEqual(response.context['form'].initial['performer'], self.member.pk)
        response = self.client.post(self.url, {'action': 'reopen', 'version': version_of(self.text),
                                               'confirm': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Można cofnąć zakończenie tylko aktualnego')
        response = self.client.post(self.url, {'action': 'performer', 'performer': self.other.pk,
                                               'version': version_of(self.text), 'confirm': 'on'})
        self.assertRedirects(response, reverse('admin:texts_text_change', args=[self.text.pk]),
                             fetch_redirect_response=False)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assigned_to, self.other)

class AdminRecordProtectionTests(StatusAssignmentFixtures):
    """Admin nie usuwa tekstów z zapisaną pracą, nie czyści przypisań z historią i nie zmienia numeru przebiegu."""

    def test_clearing_an_assignment_with_finished_work_is_refused(self):
        assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        S.objects.create(text=self.text, stage_type='editing', assignment=assignment, is_completed=True,
                         started_at=self.today, ended_at=self.today)
        with self.assertRaisesMessage(ValidationError, 'zakończone etapy'):
            correct_assignment(assignment.pk, self.admin, version_of(self.text), action='clear')
        assignment.refresh_from_db()
        self.assertEqual(assignment.assigned_to, self.member)

    def test_text_with_recorded_work_cannot_be_deleted_in_admin(self):
        from texts.admin import TextAdmin
        request = RequestFactory().get('/')
        request.user = self.admin
        model_admin = TextAdmin(Text, site)
        self.assertTrue(model_admin.has_delete_permission(request, self.text))
        S.objects.create(text=self.text, stage_type='editing', started_at=self.today)
        self.assertFalse(model_admin.has_delete_permission(request, self.text))
        self.assertNotIn('delete_selected', model_admin.get_actions(request))

    def test_deleting_text_detaches_its_source_review(self):
        from texts.admin import TextAdmin
        review = Review.objects.create(title='Źródło', length=100, anthology=self.book, status='accepted',
                                       author_first_name='A', author_last_name='B', email='a@example.test',
                                       copied_text=self.text)
        request = RequestFactory().post('/')
        request.user = self.admin
        TextAdmin(Text, site).delete_model(request, self.text)
        review.refresh_from_db()
        self.assertTrue(review.publication_detached)
        self.assertIsNone(review.copied_text_id)

    def test_cycle_cannot_be_changed_in_admin_forms(self):
        """Numer przebiegu jest w adminie tylko do odczytu – formularz nie może go zmienić."""
        assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        stage = S.objects.create(text=self.text, stage_type='editing', assignment=assignment, started_at=self.today)
        self.client.force_login(self.admin)
        for model, obj in (('workflowroleassignment', assignment), ('workflowstage', stage)):
            url = reverse(f'admin:workflow_{model}_change', args=[obj.pk])
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'name="workflow_cycle"')
            data = {'workflow_cycle': 7, 'notes': 'Uwaga', '_edit_version': form_value(page)}
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 302)
            obj.refresh_from_db()
            self.assertEqual(obj.workflow_cycle, 1)
        assignment.refresh_from_db()
        self.assertEqual(assignment.notes, 'Uwaga')
