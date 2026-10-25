"""Status i wykonawcy w adminie: ręczna zmiana statusu (także odmowy), poprawianie wykonawców (formularz tekstu,
tabela etapów, przypisanie, zbiorczo) i zasada różnych osób w kolejnych etapach."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.edit_versions import version_of
from core.selectors.texts import _text_team_members, text_detail_context
from fantazmaty.testing import create_member, form_value
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from people.models import Vacation
from texts.models import Anthology, Text
from workflow.admin_assignment_edit import correct_assignment
from workflow.admin_performers import correct_stage_performers
from workflow.admin_stage_dates import editing_transition_choices
from workflow.handoffs import eligible_handoff_users, handoff_stage
from workflow.labels import assignment_label
from workflow.models import WorkflowHandoff, WorkflowRepetition, WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.repetitions import repeat_stages
from workflow.services import STAGE_ROLES, completed_stage_exists, send_to_first_verification

from .support import add_roles, importing


class ManualStatusTests(StatusAssignmentFixtures):
    """Ręczna zmiana statusu zachowuje istniejące przypisania, ich daty i zakończoną historię."""

    def test_status_preserves_every_existing_operational_role(self):
        for kind, role in STAGE_ROLES.items():
            with self.subTest(kind=kind):
                text = Text.objects.create(title=kind, length=100, anthology=self.book)
                assignment = A.objects.create(text=text, role=role, assigned_to=self.admin)
                original_date = assignment.assigned_at
                stage = self.status(kind, text)
                assignment.refresh_from_db()
                self.assertEqual(stage.assignment_id, assignment.pk)
                self.assertEqual(stage.execution_number, assignment.execution_number)
                self.assertTrue(assignment.is_current)
                self.assertEqual(assignment.assigned_to_id, self.admin.pk)
                self.assertEqual(assignment.assigned_at, original_date)
                self.assertEqual(A.objects.filter(text=text).count(), 1)

    def test_status_preserves_downstream_team_and_completed_history(self):
        assignments = []
        for kind, role in (('first_verification', 'verifier_1'),
                           ('editing_control', 'editing_coordinator'),
                           ('first_proofreading', 'proofreader_1')):
            assignment = A.objects.create(text=self.text, role=role, assigned_to=self.member)
            stage = S.objects.create(text=self.text, stage_type=kind, assignment=assignment,
                                     started_at=self.today, ended_at=self.today, is_completed=True)
            assignments.append((assignment, stage))
        reopened = self.status('editing')
        self.assertEqual(reopened.assignment.role, 'editor')
        for assignment, stage in assignments:
            assignment.refresh_from_db()
            stage.refresh_from_db()
            self.assertTrue(assignment.is_current)
            self.assertEqual(assignment.assigned_to_id, self.member.pk)
            self.assertTrue(stage.is_completed)
            self.assertEqual(stage.ended_at, self.today)
        self.assertEqual(A.objects.filter(text=self.text).count(), 4)

    def test_admin_status_post_keeps_verifier_identity_and_role(self):
        assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        S.objects.create(text=self.text, stage_type='first_verification', assignment=assignment,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        self.client.force_login(self.admin)
        response = self.client.post(reverse('admin:texts_text_manual_status', args=[self.text.pk]),
                                    {'stage': 'editing', 'version': version_of(self.text),
                                     'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertTrue(assignment.is_current)
        self.assertEqual(assignment.assigned_to_id, self.member.pk)
        self.assertEqual(assignment_label(assignment), 'Weryfikator 1')

    def test_new_role_is_created_only_once_and_reused_on_status_correction(self):
        stage = self.status('editing_control')
        second = self.status('editing_control')
        self.assertEqual(second.assignment_id, stage.assignment_id)
        self.assertEqual(second.execution_number, 1)
        self.assertEqual(A.objects.filter(text=self.text).count(), 1)

    def test_reserved_work_still_blocks_status_correction(self):
        assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        stage = S.objects.create(text=self.text, stage_type='first_verification', assignment=assignment)
        with self.assertRaises(ValidationError):
            self.status('editing_control')
        stage.refresh_from_db()
        self.assertTrue(stage.is_current)
        self.assertEqual(A.objects.filter(text=self.text).count(), 1)

    def test_stage_performer_correction_keeps_canonical_assignment(self):
        for kind, role in (('first_verification', 'verifier_1'),
                           ('editing_control', 'editing_coordinator')):
            with self.subTest(role=role):
                assignment = A.objects.create(text=self.text, role=role, assigned_to=self.member)
                stage = S.objects.create(text=self.text, stage_type=kind, assignment=assignment)
                correct_stage_performers(self.text.pk, {stage.pk: self.other},
                                         self.admin, version_of(self.text))
                assignment.refresh_from_db()
                stage.refresh_from_db()
                self.assertEqual(stage.assignment_id, assignment.pk)
                self.assertEqual(assignment.role, role)
                self.assertEqual(assignment.assigned_to_id, self.other.pk)
                self.assertEqual(A.objects.filter(text=self.text, role=role).count(), 1)

    def test_team_labels_keep_role_names_in_current_and_previous_executions(self):
        current = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member,
                                    execution_number=2)
        old = A.objects.create(text=self.text, role='editing_coordinator', assigned_to=self.other,
                                is_current=False)
        members = _text_team_members(self.text, {'verifier_1': current})
        self.assertEqual(next(row['label'] for row in members if row['role'] == current.role),
                         'Weryfikator 1 (wyk. 2)')
        self.assertEqual(next(row['label'] for row in members if row.get('is_previous')),
                         old.get_role_display())
        self.assertFalse(any(row['label'].startswith(('Pierwsza weryfikacja', 'Kontrola K. redakcji'))
                             for row in members))

    def test_refusals_change_nothing(self):
        S.objects.create(text=self.text, stage_type='editing', started_at=self.today,
                         ended_at=self.today, is_completed=True,
                         assignment=A.objects.create(text=self.text, role='editor', assigned_to=self.member))
        with self.assertRaisesMessage(ValidationError, 'Nieprawidłowy etap'):
            self.status('editing_review')
        repeat_stages(self.text, ['editing'], self.admin)
        with self.assertRaisesMessage(ValidationError, 'aktywną kolejkę powtórzeń'):
            self.status('first_proofreading')
        self.assertFalse(S.objects.filter(text=self.text, stage_type='first_proofreading').exists())

    def test_statuses_without_performer_role_create_no_assignment(self):
        for kind in ('ready_for_editing', 'ready'):
            with self.subTest(kind=kind):
                stage = self.status(kind)
                self.assertEqual(stage.stage_type, kind)
                self.assertIsNone(stage.assignment_id)
                self.assertEqual(stage.execution_number, 1)
                self.assertFalse(A.objects.filter(text=self.text).exists())
                self.assertEqual(list(S.objects.current_cycle().filter(text=self.text)
                                      .values_list('stage_type', flat=True)), [kind])


class PerformerCorrectionTests(TestCase):
    """Poprawianie wykonawców (formularz tekstu, przypisanie, zbiorczo): różne osoby w kolejnych korektach i weryfikacjach."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('admin', 'admin@example.com', 'test')
        cls.first = User.objects.create_superuser('first', 'first@example.com', 'test')
        cls.later = User.objects.create_superuser('later', 'later@example.com', 'test')
        cls.ordinary = create_member('ordinary', 'Korektor')
        cls.book = Anthology.objects.create(title="Test")
        cls.text = Text.objects.create(title="Tekst", length=100, anthology=cls.book)
        author = Author.objects.create(first_name="Jan", last_name="Autor", email="author@example.com")
        cls.text.authors.add(author)
        cls.today = timezone.localdate()

    def setUp(self):
        self.client.force_login(self.admin)

    def pair(self, kind="second_proofreading"):
        a1 = A.objects.create(text=self.text, role="proofreader_1", assigned_to=self.first)
        a2 = A.objects.create(
            text=self.text,
            role="proofreader_2" if kind == "second_proofreading" else "proofreader_3",
            assigned_to=self.later,
        )
        s1 = S.objects.create(
            text=self.text,
            stage_type="first_proofreading",
            assignment=a1,
            started_at=self.today,
            ended_at=self.today,
            is_completed=True,
        )
        s2 = S.objects.create(text=self.text, stage_type=kind, assignment=a2, started_at=self.today)
        return a1, a2, s1, s2

    def close_book(self, s2):
        s2.ended_at = self.today
        s2.is_completed = True
        s2.save()
        S.objects.create(
            text=self.text,
            stage_type="ready",
            started_at=self.today,
            ended_at=self.today,
            is_completed=True,
        )
        self.book.status = "ready"
        self.book.save()

    def inline_payload(self, changes):
        url = reverse("admin:texts_text_change", args=[self.text.pk])
        response = self.client.get(url)
        form = response.context["adminform"].form
        data = {name: form[name].value() for name in form.fields if form[name].value() is not None}
        data.update(
            authors=list(self.text.authors.values_list("pk", flat=True)),
            _save="Save",
            _edit_version=form_value(response),
        )
        for inline in response.context["inline_admin_formsets"]:
            fs = inline.formset
            prefix = fs.prefix
            if fs.model is S:
                data[prefix + "-TOTAL_FORMS"] = str(fs.total_form_count())
                data[prefix + "-INITIAL_FORMS"] = str(fs.initial_form_count())
                for i, subform in enumerate(fs.forms):
                    stage = subform.instance
                    data[f"{prefix}-{i}-id"] = str(stage.pk)
                    data[f"{prefix}-{i}-text"] = str(self.text.pk)
                    data[f"{prefix}-{i}-performer"] = str(
                        changes.get(
                            stage.pk, stage.assignment.assigned_to_id if stage.assignment else ""
                        )
                        or ""
                    )
                    data[f"{prefix}-{i}-workflow_version"] = str(version_of(self.text))
            else:
                data[prefix + "-TOTAL_FORMS"] = "0"
                data[prefix + "-INITIAL_FORMS"] = "0"
        return url, data

    def test_batch_cannot_assign_first_and_later_to_same_person(self):
        for kind in ("second_proofreading", "third_proofreading"):
            with self.subTest(kind=kind):
                # Each example has its own workflow.
                if kind == "third_proofreading":
                    self.text = Text.objects.create(
                        title="Drugi tekst", length=100, anthology=self.book
                    )
                a1, a2, s1, s2 = self.pair(kind)
                with self.assertRaises(ValidationError):
                    correct_stage_performers(
                        self.text.pk,
                        {s1.pk: self.admin, s2.pk: self.admin},
                        self.admin,
                        version_of(self.text),
                    )
                a1.refresh_from_db()
                a2.refresh_from_db()
                self.assertEqual(a1.assigned_to_id, self.first.pk)
                self.assertEqual(a2.assigned_to_id, self.later.pk)

    def test_batch_can_swap_without_false_conflict_with_old_data(self):
        a1, a2, s1, s2 = self.pair()
        correct_stage_performers(
            self.text.pk, {s1.pk: self.later, s2.pk: self.first}, self.admin, version_of(self.text)
        )
        a1.refresh_from_db()
        a2.refresh_from_db()
        self.assertEqual(a1.assigned_to_id, self.later.pk)
        self.assertEqual(a2.assigned_to_id, self.first.pk)

    def test_swap_verifiers_atomically(self):
        a1 = A.objects.create(text=self.text, role="verifier_1", assigned_to=self.first)
        a2 = A.objects.create(text=self.text, role="verifier_2", assigned_to=self.later)
        s1 = S.objects.create(text=self.text, stage_type="first_verification", assignment=a1)
        s2 = S.objects.create(text=self.text, stage_type="second_verification", assignment=a2)
        correct_stage_performers(
            self.text.pk, {s1.pk: self.later, s2.pk: self.first}, self.admin, version_of(self.text)
        )
        a1.refresh_from_db()
        a2.refresh_from_db()
        self.assertEqual(a1.assigned_to_id, self.later.pk)
        self.assertEqual(a2.assigned_to_id, self.first.pk)
        with self.assertRaises(ValidationError):
            correct_stage_performers(self.text.pk, {s2.pk: self.later}, self.admin, version_of(self.text))
        a2.refresh_from_db()
        self.assertEqual(a2.assigned_to_id, self.first.pk)

    def test_first_proofreader_cannot_take_third_by_handoff_or_admin_correction(self):
        proofreader = create_member('third', 'Korektor')
        first = A.objects.create(text=self.text, role="proofreader_1", assigned_to=self.ordinary)
        S.objects.create(text=self.text, stage_type="first_proofreading", assignment=first,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        third = A.objects.create(text=self.text, role="proofreader_3", assigned_to=proofreader)
        s = S.objects.create(text=self.text, stage_type="third_proofreading", assignment=third,
                             started_at=self.today)
        self.assertFalse(eligible_handoff_users(s).filter(pk=self.ordinary.pk).exists())
        with self.assertRaises(ValidationError):
            handoff_stage(self.text, self.admin, stage_id=s.pk, assigned_to_id=self.ordinary.pk,
                          expected_assignment_id=s.assignment_id, reason="Test")
        with self.assertRaises(ValidationError):
            correct_stage_performers(self.text.pk, {s.pk: self.ordinary}, self.admin, version_of(self.text))
        third.refresh_from_db()
        self.assertEqual(third.assigned_to_id, proofreader.pk)

    def test_changing_completed_first_cannot_conflict_with_current_later(self):
        a1, a2, s1, s2 = self.pair()
        for edit in (
            lambda: correct_stage_performers(
                self.text.pk, {s1.pk: self.later}, self.admin, version_of(self.text)
            ),
            lambda: correct_assignment(
                a1.pk, self.admin, version_of(self.text), action="performer", performer=self.later
            ),
        ):
            with self.assertRaises(ValidationError):
                edit()
        a1.refresh_from_db()
        self.assertEqual(a1.assigned_to_id, self.first.pk)

    def test_closed_anthology_blocks_inline_and_assignment_operations(self):
        a1, a2, s1, s2 = self.pair()
        self.close_book(s2)
        operations = (
            lambda: correct_stage_performers(
                self.text.pk, {s1.pk: self.admin}, self.admin, version_of(self.text)
            ),
            lambda: correct_assignment(
                a1.pk, self.admin, version_of(self.text), action="performer", performer=self.admin
            ),
            lambda: correct_assignment(a1.pk, self.admin, version_of(self.text), action="clear"),
        )
        for operation in operations:
            with self.assertRaises(ValidationError):
                operation()
        a1.refresh_from_db()
        self.assertEqual(a1.assigned_to_id, self.first.pk)

    def test_admin_correction_accepts_person_without_current_role(self):
        a1, a2, s1, s2 = self.pair()
        self.ordinary.person_profile.roles.clear()
        url, data = self.inline_payload({s2.pk: self.ordinary.pk})
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        a2.refresh_from_db()
        self.assertEqual(a2.assigned_to_id, self.ordinary.pk)

    def test_closed_anthology_inline_post_reports_form_error(self):
        a1, a2, s1, s2 = self.pair()
        self.close_book(s2)
        url, data = self.inline_payload({s1.pk: self.admin.pk})
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Antologia jest gotowa")
        a1.refresh_from_db()
        self.assertEqual(a1.assigned_to_id, self.first.pk)

    def test_assignment_correction_accepts_former_role(self):
        a1, a2, s1, s2 = self.pair()
        response = self.client.post(
            reverse("admin:workflow_assignment_correct", args=[a2.pk]),
            {
                "action": "performer",
                "performer": self.ordinary.pk,
                "confirm": "on",
                "version": version_of(self.text),
            },
        )
        self.assertEqual(response.status_code, 302)
        a2.refresh_from_db()
        self.assertEqual(a2.assigned_to_id, self.ordinary.pk)

    def test_imported_dates_cannot_be_edited_in_closed_anthology(self):
        a1, a2, s1, s2 = self.pair()
        self.close_book(s2)
        with importing():
            s1.imported_completed = True
            s1.save()
        url = reverse("admin:workflow_workflowstage_change", args=[s1.pk])
        page = self.client.get(url)
        token = form_value(page)
        response = self.client.post(
            url,
            {
                "_edit_version": token,
                "started_at": "2026-01-01",
                "ended_at": "2026-01-02",
                "confirm_data_correction": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Antologia jest gotowa")
        s1.refresh_from_db()
        self.assertEqual(s1.started_at, self.today)



class PerformerCorrectionRulesTests(StatusAssignmentFixtures):
    """Poprawka wykonawców sprawdza cały formularz: aktualność wierszy, wspólne przypisania, przekazania i trwającą pracę."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        add_roles(cls.member, 'Redaktor', 'Korektor')
        add_roles(cls.other, 'Redaktor', 'Korektor')
        cls.editor = A.objects.create(text=cls.text, role='editor', assigned_to=cls.member)
        cls.editing = S.objects.create(text=cls.text, stage_type='editing', assignment=cls.editor,
                                       started_at=cls.today)

    def correct(self, changes, version=None):
        return correct_stage_performers(self.text.pk, changes, self.admin,
                                        version_of(self.text) if version is None else version)

    def assert_refused(self, changes, message):
        with self.assertRaisesMessage(ValidationError, message):
            self.correct(changes)
        self.editor.refresh_from_db()
        self.assertEqual(self.editor.assigned_to, self.member)

    def test_live_work_needs_an_active_available_team_member(self):
        inactive = get_user_model().objects.create_user('inactive', is_active=False)
        account_only = get_user_model().objects.create_user('account-only')
        for person in (inactive, account_only):
            with self.subTest(person=person.username):
                self.assert_refused({self.editing.pk: person}, 'aktywnego konta')
        Vacation.objects.create(person=self.other.person_profile, start_date=self.today, until_revoked=True)
        self.assert_refused({self.editing.pk: self.other}, 'urlopu')
        styling = S.objects.create(text=self.text, stage_type='styling',
                                   assignment=A.objects.create(text=self.text, role='styling', assigned_to=self.admin))
        with self.assertRaisesMessage(ValidationError, 'Stylowanie'):
            self.correct({styling.pk: self.member})

    def test_form_rows_must_match_current_records(self):
        other_text = Text.objects.create(title='Inny', length=1, anthology=self.book)
        foreign = S.objects.create(text=other_text, stage_type='editing')
        self.assert_refused({foreign.pk: self.other}, 'Lista etapów zmieniła się')
        ready = S.objects.create(text=self.text, stage_type='ready')
        self.assert_refused({ready.pk: self.other}, 'nie ma wykonawcy')
        with self.assertRaisesMessage(ValidationError, 'Dane zmieniły się'):
            self.correct({self.editing.pk: self.other}, version=version_of(self.text) - 1)
        ready.delete()
        verification = S.objects.create(text=self.text, stage_type='first_verification', assignment=self.editor,
                                        is_current=False, is_released=False)
        self.assert_refused({verification.pk: self.other}, 'nie odpowiada etapowi')

    def test_rows_sharing_one_assignment_must_name_the_same_person(self):
        author = S.objects.create(text=self.text, stage_type='author_editing', assignment=self.editor,
                                  started_at=self.today - timedelta(days=3), ended_at=self.today - timedelta(days=1),
                                  is_completed=True, is_current=False)
        self.assert_refused({self.editing.pk: self.other, author.pk: self.admin}, 'jednego przypisania')
        self.correct({self.editing.pk: self.other, author.pk: self.other})
        self.editor.refresh_from_db()
        self.assertEqual(self.editor.assigned_to, self.other)
        self.assertEqual(set(S.objects.filter(text=self.text).values_list('assignment_id', flat=True)), {self.editor.pk})

    def test_handoff_participants_and_dated_work_keep_their_people(self):
        self.assert_refused({self.editing.pk: None}, 'Praca ma już daty')
        replacement = A.objects.create(text=self.text, role='editor', assigned_to=self.other,
                                       execution_number=2, is_current=False)
        WorkflowHandoff.objects.create(text=self.text, stage=self.editing, previous_assignment=self.editor,
                                       new_assignment=replacement, actor=self.admin, reason='Test')
        self.assert_refused({self.editing.pk: self.other}, 'zarejestrowane przekazanie')

    def test_stage_without_assignment_gets_one_without_assignment_date(self):
        S.objects.filter(pk=self.editing.pk).update(ended_at=self.today, is_completed=True)
        A.objects.create(text=self.text, role='proofreader_2')
        proof = S.objects.create(text=self.text, stage_type='first_proofreading')
        self.assertIsNone(proof.assignment_id)
        self.correct({proof.pk: self.other})
        proof.refresh_from_db()
        self.assertEqual(proof.assignment.assigned_to, self.other)
        self.assertEqual(proof.assignment.execution_number, 1)
        self.assertTrue(proof.assignment.is_current)
        self.assertIsNone(proof.assignment.assigned_at)
        self.assertIsNone(proof.started_at)

class AssignmentMergeTests(StatusAssignmentFixtures):
    """Poprawka wykonawcy scala pusty bieżący przydział z historycznym tej samej roli."""

    def duplicate(self):
        empty = A.objects.create(text=self.text, role='verifier_1')
        old = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member,
                               execution_number=2, is_current=False)
        with importing():
            stage = S.objects.create(text=self.text, stage_type='first_verification',
                                     assignment=old, is_completed=True, imported_completed=True, is_current=False,
                                     is_released=False)
        return empty, old, stage

    def test_admin_replaces_person_and_merges_empty_and_historical_role(self):
        empty, old, stage = self.duplicate()
        correct_stage_performers(self.text.pk, {stage.pk: self.other}, self.admin,
                                 version_of(self.text))
        empty.refresh_from_db()
        stage.refresh_from_db()
        self.assertEqual(A.objects.filter(text=self.text, role='verifier_1').count(), 1)
        self.assertEqual(empty.assigned_to_id, self.other.pk)
        self.assertTrue(empty.is_current)
        self.assertEqual(stage.assignment_id, empty.pk)
        self.assertTrue(stage.is_completed)
        self.assertFalse(stage.is_current)
        self.assertIsNone(stage.ended_at)

    def test_archived_dateless_verification_blocks_first_and_offers_second(self):
        self.duplicate()
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.admin)
        editing = S.objects.create(text=self.text, stage_type='editing', assignment=editor,
                                   started_at=self.today)
        context = text_detail_context(user=self.admin, text=self.text)
        self.assertFalse(context['can_send_to_first_verification'])
        self.assertTrue(context['can_send_to_second_verification'])
        self.assertIn('second_verification', dict(editing_transition_choices(self.text)))
        with self.assertRaises(ValidationError):
            send_to_first_verification(self.text, self.admin)
        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)

    def test_reservation_alone_does_not_mark_verification_completed(self):
        assignment = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.member)
        S.objects.create(text=self.text, stage_type='first_verification', assignment=assignment)
        self.assertFalse(completed_stage_exists(self.text, 'first_verification'))

    def test_editor_exchange_history_survives_assignment_merge(self):
        first = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        second = A.objects.create(text=self.text, role='editor', assigned_to=self.other,
                                  execution_number=2, is_current=False)
        rows = []
        for index, kind in enumerate(('editing', 'author_editing', 'editing'), 1):
            rows.append(S.objects.create(text=self.text, stage_type=kind, assignment=second,
                        iteration=index, execution_number=2, started_at=self.today,
                        ended_at=self.today, is_completed=True, is_current=False))
        before = list(S.objects.filter(text=self.text).values_list(
            'pk', 'stage_type', 'iteration', 'execution_number', 'started_at', 'ended_at'))
        correct_stage_performers(self.text.pk, {rows[0].pk: self.admin}, self.admin,
                                 version_of(self.text))
        self.assertEqual(before, list(S.objects.filter(text=self.text).values_list(
            'pk', 'stage_type', 'iteration', 'execution_number', 'started_at', 'ended_at')))
        self.assertEqual(set(S.objects.filter(text=self.text).values_list('assignment_id', flat=True)),
                         {first.pk})


class StagePerformerCorrectionTests(StatusAssignmentFixtures):
    """Korekta wykonawcy etapu przez correct_stage_performers: powtórzenia, niespójne powiązania i pusta osoba."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        add_roles(cls.member, 'Redaktor', 'Korektor', 'Weryfikator')
        add_roles(cls.other, 'Redaktor', 'Korektor', 'Weryfikator')

    def correct(self, changes):
        correct_stage_performers(self.text.pk, changes, self.admin, version_of(self.text))

    def done(self, kind, assignment, **extra):
        return S.objects.create(text=self.text, stage_type=kind, assignment=assignment, started_at=self.today,
                                ended_at=self.today, is_completed=True, **extra)

    def test_empty_change_set_writes_nothing(self):
        version = version_of(self.text)
        self.correct({})
        self.assertEqual(version_of(self.text), version)

    def test_repetition_stages_keep_their_own_assignment(self):
        """Etapy powtórzenia nie są scalane – zmienia się osoba w przypisaniu powtórzenia, także dla
        dwóch rund redakcji tej kolejki naraz."""
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        verifier = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.other)
        original = self.done('editing', editor)
        self.done('first_verification', verifier)
        run = repeat_stages(self.text, ['editing', 'first_verification'], self.admin)
        rounds = list(run.stages.filter(stage_type='editing').order_by('queue_position'))
        self.assertEqual(len(rounds), 2)
        repeat_editor = rounds[0].assignment
        self.correct({rounds[0].pk: self.other, rounds[1].pk: self.other})
        repeat_editor.refresh_from_db()
        self.assertEqual(repeat_editor.assigned_to, self.other)
        self.assertEqual({stage.assignment_id for stage in run.stages.filter(stage_type='editing')},
                         {repeat_editor.pk})
        # Wcześniejsza redakcja (sprzed powtórzenia) nie jest scalana z przypisaniem powtórzenia.
        self.correct({original.pk: self.admin})
        editor.refresh_from_db()
        repeat_editor.refresh_from_db()
        self.assertEqual(editor.assigned_to, self.admin)
        self.assertEqual(repeat_editor.assigned_to, self.other)

    def test_history_and_inconsistent_links_prevent_merging(self):
        """Przypisania wskazane w historii powtórzeń albo powiązane z etapem innej roli nie są scalane;
        korekta zmienia tylko przypisanie poprawianego etapu."""
        cases = ('historia powtórzeń', 'etap innej roli')
        for label in cases:
            with self.subTest(label):
                S.objects.filter(text=self.text).delete()
                A.objects.filter(text=self.text).delete()
                WorkflowRepetition.objects.filter(text=self.text).delete()
                current = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
                old = A.objects.create(text=self.text, role='editor', assigned_to=self.member,
                                       execution_number=2, is_current=False)
                stage = self.done('editing', old, is_current=False)
                if label == 'historia powtórzeń':
                    # Wcześniejsza, niezwiązana kolejka nie blokuje; liczy się ta, która wskazuje przydział.
                    WorkflowRepetition.objects.create(text=self.text, canceled_at=timezone.now())
                    WorkflowRepetition.objects.create(text=self.text, canceled_at=timezone.now(),
                                                      previous_assignment_ids=[old.pk])
                else:
                    S.objects.create(text=self.text, stage_type='first_verification', assignment=old,
                                     is_current=False, is_released=False)
                self.correct({stage.pk: self.other})
                old.refresh_from_db()
                current.refresh_from_db()
                self.assertEqual(old.assigned_to, self.other)
                self.assertEqual(current.assigned_to, self.member)
                self.assertEqual(A.objects.filter(text=self.text, role='editor').count(), 2)

    def test_clearing_performer_of_undated_work(self):
        """Pusta osoba: niezaczęta praca traci wykonawcę, etap bez przypisania pozostaje bez niego;
        pusta rola W1 nie jest konfliktem weryfikatorów."""
        A.objects.create(text=self.text, role='verifier_1')
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        planned = S.objects.create(text=self.text, stage_type='editing', assignment=editor)
        loose = S.objects.create(text=self.text, stage_type='first_proofreading')
        self.correct({planned.pk: None, loose.pk: None})
        editor.refresh_from_db()
        loose.refresh_from_db()
        self.assertIsNone(editor.assigned_to_id)
        self.assertIsNone(loose.assignment_id)
        self.assertFalse(A.objects.filter(text=self.text, role='proofreader_1').exists())

    def test_completed_later_proofreading_does_not_block_first_correction(self):
        first = A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.member)
        second = A.objects.create(text=self.text, role='proofreader_2', assigned_to=self.other)
        stage = self.done('first_proofreading', first)
        self.done('second_proofreading', second)
        self.correct({stage.pk: self.other})
        first.refresh_from_db()
        self.assertEqual(first.assigned_to, self.other)


class TextStageInlineTests(StatusAssignmentFixtures):
    """Tabela etapów na stronie tekstu w adminie: wykonawca wiersza bez przydziału, wersja formularza, zapytania."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        add_roles(cls.member, 'Redaktor')
        cls.editor = A.objects.create(text=cls.text, role='editor', assigned_to=cls.member)
        cls.editing = S.objects.create(text=cls.text, stage_type='editing', assignment=cls.editor,
                                       started_at=cls.today)

    def setUp(self):
        super().setUp()
        self.url = reverse('admin:texts_text_change', args=[self.text.pk])
        self.client.force_login(self.admin)

    def stage_formset(self, response):
        return next(inline.formset for inline in response.context['inline_admin_formsets']
                    if inline.formset.model is S)

    def payload(self, response, changes):
        form = response.context['adminform'].form
        data = {name: form[name].value() for name in form.fields if form[name].value() is not None}
        data.update(_save='Zapisz', _edit_version=form_value(response))
        for inline in response.context['inline_admin_formsets']:
            formset = inline.formset
            prefix = formset.prefix
            data[prefix + '-TOTAL_FORMS'] = str(formset.total_form_count() if formset.model is S else 0)
            data[prefix + '-INITIAL_FORMS'] = str(formset.initial_form_count() if formset.model is S else 0)
            if formset.model is not S:
                continue
            for index, subform in enumerate(formset.forms):
                stage = subform.instance
                data[f'{prefix}-{index}-id'] = str(stage.pk)
                data[f'{prefix}-{index}-text'] = str(self.text.pk)
                performer = changes.get(stage.pk, stage.assignment.assigned_to_id if stage.assignment else '')
                data[f'{prefix}-{index}-performer'] = str(performer or '')
                data[f'{prefix}-{index}-workflow_version'] = str(version_of(self.text))
        return data

    def test_row_without_assignment_shows_role_performer_and_stale_version_is_refused(self):
        proofreader = A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.other)
        loose = S.objects.create(text=self.text, stage_type='first_proofreading')
        # Dane masowe (np. stary import) mogą nie mieć powiązania etapu z bieżącym przypisaniem roli.
        S.objects.filter(pk=loose.pk).update(assignment=None)
        response = self.client.get(self.url)
        forms = {form.instance.pk: form for form in self.stage_formset(response).forms}
        self.assertEqual(forms[loose.pk].fields['performer'].initial, self.other.pk)
        data = self.payload(response, {self.editing.pk: self.other.pk})
        prefix = self.stage_formset(response).prefix
        for index in range(len(forms)):
            data[f'{prefix}-{index}-workflow_version'] = str(version_of(self.text) - 1)
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Workflow zmienił się od otwarcia strony', ' '.join(self.stage_formset(response).non_form_errors()))
        self.editor.refresh_from_db()
        proofreader.refresh_from_db()
        self.assertEqual(self.editor.assigned_to, self.member)
        self.assertEqual(proofreader.assigned_to, self.other)

    def test_stage_rows_use_a_constant_number_of_queries(self):
        """Wiersze tabeli etapów: wersję tekstu i bieżące przydziały ról formset pobiera raz,
        niezależnie od liczby etapów, także bez powiązanego przydziału."""
        from django.contrib import admin
        from django.test import RequestFactory
        from texts.admin import WorkflowStageInline
        from texts.models import Text

        request = RequestFactory().get(self.url)
        request.user = self.admin
        inline = WorkflowStageInline(Text, admin.site)
        A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.other)
        for kind in ('first_proofreading', 'second_proofreading', 'third_proofreading'):
            loose = S.objects.create(text=self.text, stage_type=kind)
            S.objects.filter(pk=loose.pk).update(assignment=None)
            formset = inline.get_formset(request, self.text)(instance=self.text,
                                                             queryset=inline.get_queryset(request))
            with self.subTest(kind=kind), self.assertNumQueries(3):
                forms = formset.forms
            self.assertEqual(forms[-1].fields['workflow_version'].initial, version_of(self.text))
