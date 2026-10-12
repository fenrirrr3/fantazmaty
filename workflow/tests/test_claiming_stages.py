"""Przejmowanie etapów: formularze na liście i w szczegółach, rezerwacje, kandydaci, powody braku możliwości
przejęcia oraz blokady zakończonych weryfikacji."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.selectors.texts import _annotated_texts, text_detail_context, text_list_context
from core.services.texts import start_assigned_stage
from core.translation_scope import frontend_scope
from fantazmaty.testing import create_member, create_superuser
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from people.models import Person, Vacation
from texts.models import Anthology, Text
from workflow.availability import claim_access, claim_reason, eligible_role_users, role_access_reason
from workflow.models import WorkflowRepetition, WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.read_queries import available_stages
from workflow.repetitions import repeat_stages
from workflow.services import (
    claim_ready_for_editing, claim_stage, complete_stage, create_pending_stage, resume_editing,
    send_to_first_verification, send_to_second_verification, start_first_verification,
)
from workflow.state import current_stage

from .support import AnthologyWorkflowMixin, add_roles, importing


class ClaimFormsTests(AnthologyWorkflowMixin, TestCase):
    """Formularz „Przejmij” w szczegółach tekstu i na liście tekstów do wzięcia."""

    def pending_verification(self):
        """Redakcja w toku i oczekująca pierwsza weryfikacja; zalogowany pierwszy weryfikator."""
        self.begin_editing()
        self.client.force_login(self.verifier_1)
        return self.stage('first_verification')

    def test_reservation_has_no_date_in_list_and_detail_and_stays_unstarted(self):
        pending = self.pending_verification()
        for route in [reverse('core:available_texts'), reverse('core:assigned_text_detail', args=[self.text.pk])]:
            form = self.claim_form(self.client.get(route), pending)
            self.assertEqual(form.xpath('normalize-space(.//button)'), 'Przejmij')
            self.assertIn('Obecnie tylko przypisanie', form.text_content())
            self.assertFalse(form.xpath('.//input[@name="started_at"]'))
        data = {node.get('name'): node.get('value', '') for node in form.xpath('.//input[@name]')}
        response = self.client.post(form.get('action'), data)
        self.assertEqual(response.status_code, 302)
        pending.refresh_from_db()
        self.assertIsNone(pending.started_at)
        self.assertEqual(pending.assignment.assigned_to_id, self.verifier_1.pk)
        self.assertFalse(self.detail(self.verifier_2).context['claimable_stages'])

    def test_handoff_exposes_date_and_post_honors_chosen_date(self):
        pending = self.pending_verification()
        send_to_first_verification(self.text, self.editor, self.today)
        for route in [reverse('core:available_texts'), reverse('core:assigned_text_detail', args=[self.text.pk])]:
            form = self.claim_form(self.client.get(route), pending)
            self.assertTrue(form.xpath('.//input[@name="started_at"]'))
            self.assertNotIn('Obecnie tylko przypisanie', form.text_content())
        date = self.today + timedelta(days=2)
        response = self.client.post(reverse('core:take_workflow_stage', args=[pending.pk]), {'started_at': date.isoformat()})
        self.assertEqual(response.status_code, 302)
        pending.refresh_from_db()
        self.assertEqual(pending.started_at, date)

    def test_date_before_handoff_is_rejected_without_assignment(self):
        pending = self.pending_verification()
        with self.assertRaises(ValidationError):
            claim_stage(self.text, 'first_verification', self.verifier_1, self.today)
        self.client.post(reverse('core:take_workflow_stage', args=[pending.pk]), {'started_at': self.today.isoformat()})
        pending.refresh_from_db()
        self.assertIsNone(pending.started_at)
        self.assertIsNone(pending.assignment_id)

    def test_detail_claims_same_stage_as_available_list_and_submits_with_version(self):
        response = self.detail(self.editor)
        form = self.claim_form(response, self.initial_stage)
        self.assertEqual([r['pk'] for r in response.context['claimable_stages']],
            list(available_stages(self.editor, claim_access(self.editor)).filter(text=self.text).values_list('pk', flat=True)))
        data = {node.get('name'): node.get('value', '') for node in form.xpath('.//input[@name]')}
        self.assertIn('_edit_version', data)
        self.assertEqual(data['started_at'], self.today.isoformat())
        taken = self.client.post(form.get('action'), data)
        self.assertRedirects(taken, reverse('core:assigned_text_detail', args=[self.text.pk]), fetch_redirect_response=False)
        self.assertEqual(A.objects.current_cycle().get(text=self.text, role='editor').assigned_to, self.editor)
        self.assertEqual(self.stage('editing').started_at, self.today)
        self.assertFalse(self.detail().context['claimable_stages'])

    def test_preassigned_verifier_keeps_first_button_and_blocks_second_before_completion(self):
        editing = self.begin_editing()
        first = claim_stage(self.text, 'first_verification', self.verifier_1)
        response = self.detail()
        self.assertContains(response, 'Przekaż do pierwszej weryfikacji')
        self.assertNotContains(response, 'Przekaż do drugiej weryfikacji')
        with self.assertRaisesMessage(ValidationError, 'Najpierw należy zakończyć pierwszą'):
            send_to_second_verification(self.text, self.editor, self.today)
        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)
        send_to_first_verification(self.text, self.editor, self.today)
        first = start_first_verification(self.text, self.verifier_1, self.today)
        complete_stage(first, self.verifier_1, self.today)
        resume_editing(self.text, self.editor, self.today)
        response = self.detail()
        self.assertNotContains(response, 'Przekaż do pierwszej weryfikacji')
        self.assertContains(response, 'Przekaż do drugiej weryfikacji')

    def test_claim_controls_are_hidden_for_wrong_role_leave_and_closed_anthology(self):
        self.assertFalse(text_detail_context(user=self.proofreader, text=self.text)['claimable_stages'])
        Vacation.objects.create(person=self.editor.person_profile, start_date=self.today, until_revoked=True)
        self.assertFalse(text_detail_context(user=self.editor, text=self.text)['claimable_stages'])
        # Older imports may contain a closed anthology with an open text.
        Anthology.objects.filter(pk=self.text.anthology_id).update(status='ready')
        self.assertFalse(text_detail_context(user=self.other_editor, text=self.text)['claimable_stages'])


class CompletedVerificationClaimTests(TestCase):
    """Zakończona (także importowana bez dat) weryfikacja nie wraca do przejęcia jako pusty etap."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.user = create_member('member', 'Redaktor')
        add_roles(cls.user, 'Weryfikator', 'Korektor')
        cls.other = create_member('other', 'Redaktor')
        cls.admin = create_superuser()
        cls.text = Text.objects.create(title='Stary tekst', length=100,
                                       anthology=Anthology.objects.create(title='Antologia'))

    def setUp(self):
        self.client.force_login(self.user)

    def imported(self, kind, *, role=None, current=True):
        assignment = (A.objects.create(text=self.text, role=role, assigned_to=self.other,
                                        is_current=False) if role else None)
        with importing():
            return S.objects.create(text=self.text, stage_type=kind, assignment=assignment,
                is_completed=True, imported_completed=True, is_current=current,
                started_at=None, ended_at=None)

    def obsolete_first(self):
        completed = self.imported('first_verification', role='verifier_1')
        self.imported('editing')
        pending = S.objects.create(text=self.text, stage_type='first_verification', iteration=2)
        return completed, pending

    def test_completed_first_hides_legacy_pending_row_and_blocks_claim_policy(self):
        completed, pending = self.obsolete_first()
        self.assertFalse(available_stages(self.user, claim_access(self.user)).filter(pk=pending.pk).exists())
        reason = claim_reason(pending, self.user,
            list(S.objects.current_cycle().filter(text=self.text)),
            list(A.objects.current_cycle().filter(text=self.text)))
        self.assertIn('już zakończona', reason)
        with self.assertRaisesMessage(ValidationError, 'już zakończona'):
            claim_stage(self.text, 'first_verification', self.user)
        completed.refresh_from_db()
        self.assertTrue(completed.is_completed)
        self.assertIsNone(completed.started_at)
        self.assertIsNone(completed.ended_at)

    def test_click_on_obsolete_first_returns_message_without_500_or_new_assignment(self):
        completed, pending = self.obsolete_first()
        response = self.client.post(reverse('core:take_workflow_stage', args=[pending.pk]))
        self.assertRedirects(response, reverse('core:available_texts'), fetch_redirect_response=False)
        messages = [str(message) for message in response.wsgi_request._messages]
        self.assertTrue(any('już zakończona' in message for message in messages))
        pending.refresh_from_db()
        self.assertIsNone(pending.assignment_id)
        self.assertIsNone(pending.started_at)
        self.assertEqual(A.objects.filter(text=self.text, role='verifier_1').count(), 1)
        completed.refresh_from_db()
        self.assertEqual(completed.assignment.assigned_to_id, self.other.pk)

    def test_automatic_editing_claim_keeps_completed_first_without_creating_another(self):
        completed = self.imported('first_verification', role='verifier_1')
        S.objects.create(text=self.text, stage_type='ready_for_editing')
        editing = claim_ready_for_editing(self.text, self.user)
        self.assertEqual(editing.stage_type, 'editing')
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)
        completed.refresh_from_db()
        self.assertTrue(completed.is_completed)
        self.assertIsNone(completed.started_at)

    def test_trusted_pending_creation_cannot_repeat_completed_verification(self):
        self.imported('first_verification', role='verifier_1')
        with self.assertRaisesMessage(ValidationError, 'już zakończona'):
            create_pending_stage(self.text, 'first_verification')
        self.assertEqual(S.objects.filter(text=self.text, stage_type='first_verification').count(), 1)

    def test_claim_with_historical_assignment_uses_next_execution_number(self):
        old = self.imported('first_proofreading', role='proofreader_1', current=False)
        pending = S.objects.create(text=self.text, stage_type='first_proofreading', iteration=2)
        response = self.client.post(reverse('core:take_workflow_stage', args=[pending.pk]),
                                     {'started_at': self.today.isoformat()})
        self.assertRedirects(response, reverse('core:assigned_text_detail', args=[self.text.pk]),
                             fetch_redirect_response=False)
        pending.refresh_from_db()
        self.assertEqual(pending.started_at, self.today)
        self.assertEqual(pending.assignment.assigned_to_id, self.user.pk)
        self.assertEqual(pending.assignment.execution_number, 2)
        old.refresh_from_db()
        self.assertEqual(old.assignment.execution_number, 1)
        self.assertEqual(old.assignment.assigned_to_id, self.other.pk)

    def test_claim_gives_open_stage_execution_number_of_new_assignment(self):
        """Nowe wykonanie przydziału (wyk. 2) nadaje ten sam numer otwartemu etapowi, więc etykieta etapu,
        etykieta przydziału i snapshot zdarzeń mówią o tym samym wykonaniu."""
        from core.workflow_events import snapshot
        from workflow.labels import assignment_label, stage_label
        self.imported('first_proofreading', role='proofreader_1', current=False)
        pending = S.objects.create(text=self.text, stage_type='first_proofreading', iteration=2)
        claimed = claim_stage(self.text, 'first_proofreading', self.user, self.today)
        self.assertEqual(claimed.pk, pending.pk)
        self.assertEqual(claimed.execution_number, 2)
        pending.refresh_from_db()
        self.assertEqual(pending.execution_number, pending.assignment.execution_number)
        self.assertEqual(stage_label(pending), 'Pierwsza korekta (wyk. 2)')
        self.assertEqual(assignment_label(pending.assignment), 'Korektor 1 (wyk. 2)')
        self.assertTrue(snapshot(self.text.pk, 'default')['stages'][pending.pk][0]
                        .startswith('Pierwsza korekta – wykonanie 2'))

    def test_old_empty_first_does_not_block_continued_editing_or_misreport_status(self):
        completed, obsolete = self.obsolete_first()
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.user)
        editing = S.objects.create(text=self.text, stage_type='editing', iteration=2, assignment=editor)
        detail = text_detail_context(user=self.user, text=self.text)
        self.assertTrue(detail['can_resume_editing'])
        self.assertEqual(current_stage(self.text.workflow_stages.all()).pk, editing.pk)
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, 'editing')
        start_assigned_stage(user=self.user, stage_id=editing.pk, started_at=self.today)
        editing.refresh_from_db()
        self.assertEqual(editing.started_at, self.today)
        detail = text_detail_context(user=self.user, text=self.text)
        self.assertFalse(detail['can_send_to_first_verification'])
        self.assertTrue(detail['can_send_to_second_verification'])
        self.assertTrue(detail['can_send_to_author'])
        obsolete.refresh_from_db()
        self.assertIsNone(obsolete.started_at)
        completed.refresh_from_db()
        self.assertTrue(completed.is_completed)

    def test_resume_ignores_empty_duplicate_but_keeps_actual_verification_gate(self):
        self.obsolete_first()
        A.objects.create(text=self.text, role='editor', assigned_to=self.user)
        editing = resume_editing(self.text, self.user)
        self.assertEqual(editing.started_at, self.today)

    def test_completed_second_is_not_available_again(self):
        self.imported('first_verification', role='verifier_1')
        self.imported('second_verification', role='verifier_2')
        self.imported('editing')
        pending = S.objects.create(text=self.text, stage_type='second_verification', iteration=2)
        self.assertFalse(available_stages(self.user, claim_access(self.user)).filter(pk=pending.pk).exists())
        with self.assertRaisesMessage(ValidationError, 'Druga weryfikacja jest już zakończona'):
            claim_stage(self.text, 'second_verification', self.user)

    def test_explicit_repetition_still_allows_another_first_verification(self):
        self.imported('first_verification', role='verifier_1')
        S.objects.create(text=self.text, stage_type='ready')
        run = repeat_stages(self.text, ['first_verification'], self.admin)
        repeated = run.stages.get()
        self.assertTrue(available_stages(self.user, claim_access(self.user)).filter(pk=repeated.pk).exists())
        response = self.client.post(reverse('core:take_workflow_stage', args=[repeated.pk]),
                                     {'started_at': self.today.isoformat()})
        self.assertRedirects(response, reverse('core:assigned_text_detail', args=[self.text.pk]),
                             fetch_redirect_response=False)
        repeated.refresh_from_db()
        self.assertEqual(repeated.started_at, self.today)
        self.assertEqual(repeated.assignment.assigned_to_id, self.user.pk)

    def test_previous_cycle_completion_does_not_block_new_first_verification(self):
        completed = self.imported('first_verification', role='verifier_1')
        self.text.current_workflow_cycle = 2
        self.text.save()
        pending = S.objects.create(text=self.text, workflow_cycle=2, stage_type='first_verification')
        self.assertTrue(available_stages(self.user, claim_access(self.user)).filter(pk=pending.pk).exists())
        claim_stage(self.text, 'first_verification', self.user, stage_id=pending.pk)
        completed.refresh_from_db()
        self.assertTrue(completed.is_completed)
        self.assertIsNone(completed.started_at)

    def test_endpoint_claims_clicked_execution_instead_of_another_pending_row(self):
        clicked = S.objects.create(text=self.text, stage_type='first_proofreading')
        other = S.objects.create(text=self.text, stage_type='first_proofreading', iteration=2)
        response = self.client.post(reverse('core:take_workflow_stage', args=[clicked.pk]),
                                     {'started_at': self.today.isoformat()})
        self.assertEqual(response.status_code, 302)
        clicked.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(clicked.started_at, self.today)
        self.assertIsNone(other.started_at)

    def test_unexpected_assignment_conflict_returns_message_and_keeps_database_unchanged(self):
        pending = S.objects.create(text=self.text, stage_type='first_proofreading')
        with patch('core.views.workflow.claim_stage', side_effect=IntegrityError('assignment conflict')):
            with self.assertLogs('core.views.workflow', level='ERROR'):
                response = self.client.post(reverse('core:take_workflow_stage', args=[pending.pk]),
                                             {'started_at': self.today.isoformat()})
        self.assertRedirects(response, reverse('core:available_texts'), fetch_redirect_response=False)
        self.assertTrue(any('Nie udało się przejąć etapu' in str(message)
                            for message in response.wsgi_request._messages))
        pending.refresh_from_db()
        self.assertIsNone(pending.started_at)
        self.assertFalse(A.objects.filter(text=self.text).exists())

    def test_first_verification_after_undated_imported_editing_starts_immediately(self):
        """Regresja: po imporcie redakcji bez dat pierwszą weryfikację dało się tylko zarezerwować."""
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.user)
        with importing():
            S.objects.create(text=self.text, stage_type='editing', assignment=editor,
                             is_completed=True, imported_completed=True)
        pending = S.objects.create(text=self.text, stage_type='first_verification')
        self.assertTrue(available_stages(self.user, claim_access(self.user)).filter(pk=pending.pk).exists())
        claim_stage(self.text, 'first_verification', self.user)
        pending.refresh_from_db()
        self.assertEqual(pending.started_at, self.today)


class CandidateEligibilityTests(StatusAssignmentFixtures):
    """Listy kandydatów (przejęcie, przekazanie, powtórzenie) zgadzają się z walidacją zapisu."""

    def test_unlinked_person_leave_does_not_empty_candidate_lists(self):
        from workflow.repetitions import eligible_repeat_users
        from workflow.handoffs import eligible_handoff_users

        add_roles(self.member, 'Redaktor')
        assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.other)
        stage = S.objects.create(text=self.text, stage_type='editing', assignment=assignment)
        before = list(eligible_repeat_users(self.text, 'editor'))
        with self.assertNumQueries(1):
            self.assertIn(self.member.pk, list(eligible_handoff_users(stage).values_list('pk', flat=True)))
        person = Person.objects.create(first_name='Leave', last_name='No account', email='leave@example.test')
        Vacation.objects.create(person=person, start_date=self.today, until_revoked=True)
        self.assertEqual(list(eligible_repeat_users(self.text, 'editor')), before)
        self.assertIn(self.member, before)
        self.assertIn(self.member, eligible_handoff_users(stage))
        Vacation.objects.create(person=self.member.person_profile, start_date=self.today, until_revoked=True)
        self.assertNotIn(self.member, eligible_repeat_users(self.text, 'editor'))
        self.assertNotIn(self.member, eligible_handoff_users(stage))

    def test_legacy_group_does_not_offer_candidate_rejected_by_write(self):
        from core.services.texts import _require_eligible_assignee
        from workflow.availability import eligible_role_users

        group = Group.objects.get_or_create(name='Redaktor')[0]
        type(self.member).groups.through.objects.create(user_id=self.member.pk, group_id=group.pk)
        self.assertNotIn(self.member, eligible_role_users('editor'))
        with self.assertRaises(ValidationError):
            _require_eligible_assignee(self.member, 'editor')
        add_roles(self.member, 'Redaktor')
        self.assertIn(self.member, eligible_role_users('editor'))
        _require_eligible_assignee(self.member, 'editor')

    def test_candidate_roles_preserve_special_proofreading_policy(self):
        from workflow.availability import eligible_role_users

        add_roles(self.member, 'Koordynator')
        self.assertIn(self.member, eligible_role_users('editor'))
        self.assertNotIn(self.member, eligible_role_users(A.Role.PROOFREADER_2))
        self.assertNotIn(self.member, eligible_role_users(A.Role.STYLING))
        add_roles(self.member, 'Koordynator korekty')
        self.assertIn(self.member, eligible_role_users(A.Role.PROOFREADER_2))
        self.assertIn(self.member, eligible_role_users(A.Role.PROOFREADER_4))
        self.assertIn(self.admin, eligible_role_users(A.Role.STYLING))


class AbandonedAnthologyTests(TestCase):
    """Porzucona antologia znika z list strony i nie da się przejąć jej tekstu."""

    @classmethod
    def setUpTestData(cls):
        cls.manager = create_member('v68manager', 'Koordynator korekty')
        cls.admin = create_superuser()
        cls.book = Anthology.objects.create(title='Porzucona antologia', status='abandoned')
        cls.text = Text.objects.create(title='Porzucony tekst', anthology=cls.book, length=1)
        cls.stage = S.objects.create(text=cls.text, stage_type='ready_for_editing', is_released=True)

    def test_abandoned_scopes_filters_admin_and_direct_claim(self):
        self.assertFalse(frontend_scope(Anthology.objects).filter(pk=self.book.pk).exists())
        self.assertFalse(frontend_scope(Text.objects).filter(pk=self.text.pk).exists())
        self.client.force_login(self.admin)
        for name in ('core:anthology_list', 'core:available_texts', 'core:text_list', 'core:workflow_list'):
            page = self.client.get(reverse(name), {'sort': 'anthology', 'hide_ready': '0'})
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, self.book.title)
        page = self.client.get(reverse('admin:texts_anthology_change', args=[self.book.pk]))
        self.assertContains(page, 'Porzucona')
        context = text_list_context(user=self.admin, params={'anthology': str(self.book.pk), 'hide_ready': '0'})
        self.assertFalse(context['filtered_queryset'].exists())
        with self.assertRaises(ValidationError):
            claim_ready_for_editing(self.text, self.manager)
        self.assertTrue(S.objects.filter(pk=self.stage.pk).exists())
        self.book.status = 'in_preparation'
        self.book.save()
        self.assertTrue(frontend_scope(Text.objects).filter(pk=self.text.pk).exists())
        self.assertContains(self.client.get(reverse('core:text_list')), self.text.title)


class ClaimReasonTests(TestCase):
    """Powód, dla którego osoba nie może przejąć etapu – ten sam dla listy „Do wzięcia” i dla zapisu."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.editor = create_member('editor', 'Redaktor')
        cls.verifier = create_member('verifier', 'Weryfikator')
        cls.proofreader = create_member('proofreader', 'Korektor')
        cls.book = Anthology.objects.create(title='Antologia')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.today = timezone.localdate()

    def reason(self, kind, user, stages=None, assignments=(), **fields):
        stage = S(text=self.text, stage_type=kind, **fields)
        return claim_reason(stage, user, [stage] if stages is None else stages, list(assignments))

    def editing(self, **fields):
        return S(text=self.text, stage_type='editing', **fields)

    def test_closed_or_unreleased_work_cannot_be_claimed(self):
        for kind, fields, stages, expected in (
                ('editing', {'is_released': False}, None, 'Etap czeka na zakończenie poprzedniego.'),
                ('editing', {}, [S(stage_type='ready')], 'Proces jest zamknięty.'),
                ('editing', {'started_at': self.today}, None, 'Etap nie oczekuje na przejęcie.'),
                ('ready', {}, [], 'Tego etapu nie można przejąć.'),
                ('editor_control', {}, [], 'Kontrolę rozpoczyna przypisany redaktor.')):
            with self.subTest(kind=kind, expected=expected):
                self.assertEqual(self.reason(kind, self.admin if kind == 'editor_control' else self.editor,
                                             stages, **fields), expected)
        for status, expected in (('ready', 'Antologia jest gotowa.'), ('abandoned', 'Antologia została porzucona.')):
            Anthology.objects.filter(pk=self.book.pk).update(status=status)
            self.text.anthology.refresh_from_db()
            self.assertEqual(self.reason('editing', self.editor), expected)

    def test_role_policy_and_leave(self):
        self.assertEqual(self.reason('first_proofreading', self.editor), 'Brak wymaganej roli.')
        self.assertEqual(self.reason('second_proofreading', self.proofreader),
                         'Druga i czwarta korekta są dostępne tylko dla koordynatora korekty.')
        self.assertEqual(self.reason('styling', self.editor), 'Stylowanie jest dostępne tylko dla superusera.')
        self.assertEqual(self.reason('styling', self.admin), '')
        Vacation.objects.create(person=self.editor.person_profile, start_date=self.today, until_revoked=True)
        self.assertEqual(self.reason('ready_for_editing', self.editor), 'Brak aktywnego dostępu do zespołu.')
        self.assertEqual(claim_access(self.editor)['member'], False)
        self.assertEqual(role_access_reason(self.editor, 'editor'), 'Brak aktywnego dostępu do zespołu albo trwa urlop.')
        account_only = get_user_model().objects.create_user('account-only')
        self.assertEqual(claim_access(account_only), {'member': False, 'coordinator': False, 'roles': set()})
        self.assertFalse(eligible_role_users('verifier_4').exists())

    def test_people_already_on_the_text(self):
        taken = A(text=self.text, role='verifier_1', assigned_to=self.admin)
        self.assertEqual(self.reason('first_verification', self.verifier, assignments=[taken]),
                         'Rola ma już przypisanego wykonawcę.')
        own_first = A(text=self.text, role='verifier_1', assigned_to=self.verifier)
        self.assertEqual(self.reason('second_verification', self.verifier, assignments=[own_first]),
                         'Pierwszą i drugą weryfikację wykonują różne osoby.')
        first = A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.admin)
        S.objects.create(text=self.text, stage_type='first_proofreading', assignment=first,
                         started_at=self.today, ended_at=self.today, is_completed=True)
        self.assertEqual(self.reason('third_proofreading', self.admin),
                         'Pierwszą korektę tego tekstu wykonywała już ta osoba.')
        self.assertEqual(self.reason('third_proofreading', self.proofreader), '')

    def test_editorial_order_between_editing_and_verifications(self):
        waiting_editing = self.editing()
        self.assertEqual(self.reason('editing', self.editor, [waiting_editing, S(stage_type='first_verification')]),
                         'Użyj przekazania lub wznowienia redakcji.')
        first = S(text=self.text, stage_type='first_verification')
        self.assertEqual(self.reason('first_verification', self.verifier, [waiting_editing, first]),
                         'Rezerwacja wymaga rozpoczętej redakcji.')
        self.assertEqual(self.reason('first_verification', self.verifier,
                                     [self.editing(started_at=self.today), first]), '')
        second = S(text=self.text, stage_type='second_verification')
        self.assertEqual(self.reason('second_verification', self.verifier, [waiting_editing, second]),
                         'Najpierw zakończ pierwszą weryfikację.')
        S.objects.create(text=self.text, stage_type='first_verification', started_at=self.today,
                         ended_at=self.today, is_completed=True, is_current=False)
        self.assertEqual(self.reason('second_verification', self.verifier,
                                     [self.editing(started_at=self.today), second]),
                         'Redaktor musi przekazać tekst do drugiej weryfikacji.')
        self.assertEqual(self.reason('second_verification', self.verifier,
                                     [self.editing(started_at=self.today, is_completed=True), second]), '')
        run = WorkflowRepetition.objects.create(text=self.text)
        self.assertEqual(self.reason('first_verification', self.verifier, [waiting_editing, first],
                                     repetition=run), '')
