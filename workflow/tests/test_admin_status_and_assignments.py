"""Status i wykonawcy w adminie: ręczna zmiana statusu, poprawianie wykonawców i zasada różnych osób w kolejnych etapach."""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.edit_versions import version_of
from core.selectors.texts import _text_team_members, text_detail_context
from fantazmaty.testing import create_member, form_value
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from texts.models import Anthology, Text
from workflow.admin_assignment_edit import correct_assignment
from workflow.admin_performers import correct_stage_performers
from workflow.admin_stage_dates import editing_transition_choices
from workflow.handoffs import eligible_handoff_users, handoff_stage
from workflow.labels import assignment_label
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.services import STAGE_ROLES, completed_stage_exists, send_to_first_verification

from .support import importing


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


class PerformerCorrectionTests(TestCase):
    """Poprawianie wykonawców (formularz tekstu, przypisanie, zbiorczo): różne osoby w kolejnych korektach i weryfikacjach."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser('admin', 'admin@example.com', 'test')
        self.first = User.objects.create_superuser('first', 'first@example.com', 'test')
        self.later = User.objects.create_superuser('later', 'later@example.com', 'test')
        self.ordinary = create_member('ordinary', 'Korektor')
        self.book = Anthology.objects.create(title="Test")
        self.text = Text.objects.create(title="Tekst", length=100, anthology=self.book)
        author = Author.objects.create(first_name="Jan", last_name="Autor", email="author@example.com")
        self.text.authors.add(author)
        self.today = timezone.localdate()
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

    def test_authorization_denial_for_actor_is_preserved(self):
        a1, a2, s1, s2 = self.pair()
        with self.assertRaises(PermissionDenied):
            correct_stage_performers(
                self.text.pk, {s2.pk: self.admin}, self.ordinary, version_of(self.text)
            )

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
