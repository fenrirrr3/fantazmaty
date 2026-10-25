"""Przejmowanie etapów: formularze na liście i w szczegółach, rezerwacje, kandydaci, powody braku możliwości
przejęcia, blokady zakończonych weryfikacji oraz blokada pobierania ustawiana przez superusera."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.selectors.texts import text_detail_context, text_list_context
from core.models import WorkflowEvent
from core.services.texts import TAKE_LOCK_EVENTS, change_scheduled_stage, start_assigned_stage, toggle_take_lock
from core.translation_scope import frontend_scope
from fantazmaty.testing import create_member, create_superuser, edit_token
from fantazmaty.testing.workflow import StatusAssignmentFixtures, start_reserved_first_verification
from people.models import Person, Vacation
from texts.models import Anthology, Text
from texts.novels import assign_chapters, edit_token as novel_token, locked_book, new_chapter
from workflow.availability import claim_access, claim_reason, eligible_role_users, role_access_reason
from workflow.models import WorkflowRepetition, WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.read_queries import available_stages
from workflow.repetitions import repeat_stages
from workflow.services import (
    claim_ready_for_editing, claim_stage, complete_stage, create_pending_stage, resume_editing,
    send_to_first_verification, send_to_second_verification, TAKE_LOCKED_MESSAGE,
)
from workflow.state import stage_is_active

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
        first = start_reserved_first_verification(self.text, self.verifier_1, self.today)
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

    def test_completed_verification_is_never_recreated_as_empty_stage(self):
        """Ani zaufane tworzenie oczekującego etapu, ani zmiana statusu w adminie nie dodają pustej W1/W2
        po zakończonej weryfikacji tego typu w bieżącym przebiegu – także gdy jej wykonanie jest już
        niebieżące (po powrocie do redakcji). Odczyty nie ukrywają takich etapów, więc nie mogą one powstać."""
        from core.edit_versions import version_of
        from workflow.admin_status import set_admin_status
        creators = {
            'create_pending_stage': lambda kind: create_pending_stage(self.text, kind),
            'set_admin_status': lambda kind: set_admin_status(self.text.pk, kind, self.admin, version_of(self.text)),
        }
        for kind, role in (('first_verification', 'verifier_1'), ('second_verification', 'verifier_2')):
            for current in (True, False):
                for name, create in creators.items():
                    with self.subTest(kind=kind, current=current, path=name), transaction.atomic():
                        self.imported(kind, role=role, current=current)
                        with self.assertRaisesMessage(ValidationError, 'już zakończona'):
                            create(kind)
                        self.assertEqual(S.objects.filter(text=self.text, stage_type=kind).count(), 1)
                        transaction.set_rollback(True)

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


class TakeLockTests(AnthologyWorkflowMixin, TestCase):
    """„Zablokuj tekst”: superuser wstrzymuje pobieranie tekstu do etapów i w tym samym miejscu je przywraca."""

    def toggle(self, user):
        self.client.force_login(user)
        return self.client.post(reverse('core:toggle_text_take_lock', args=[self.text.pk]),
                                {'_edit_version': edit_token(user, self.text)})

    def lock(self):
        toggle_take_lock(user=self.superuser, text_id=self.text.pk)

    def work_state(self):
        return (list(S.objects.filter(text=self.text).order_by('pk').values_list('pk', 'stage_type', 'started_at',
                                                                                    'is_completed', 'assignment_id')),
                list(A.objects.filter(text=self.text).order_by('pk').values_list('pk', 'role', 'assigned_to_id')))

    def test_superuser_toggles_lock_with_message_and_event_log(self):
        """Dwa kliknięcia: blokada (data, kto, komunikat, wpis w dzienniku bez zmiany statusu) i odblokowanie."""
        for expected, message in ((True, 'Zablokowano pobieranie tekstu.'), (False, 'Odblokowano pobieranie tekstu.')):
            with self.subTest(locked=expected):
                response = self.toggle(self.superuser)
                self.assertRedirects(response, reverse('core:assigned_text_detail', args=[self.text.pk]),
                                     fetch_redirect_response=False)
                self.assertEqual([str(m) for m in response.wsgi_request._messages._queued_messages], [message])
                self.text.refresh_from_db()
                self.assertEqual(self.text.take_locked, expected)
                self.assertEqual(self.text.take_locked_by, self.superuser if expected else None)
                event = WorkflowEvent.objects.filter(text=self.text).first()
                self.assertEqual(event.details, TAKE_LOCK_EVENTS[expected])
                self.assertEqual((event.actor, event.previous_status), (self.superuser, event.next_status))
        self.assertEqual(WorkflowEvent.objects.filter(text=self.text).count(), 2)

    def test_toggle_and_button_are_superuser_only(self):
        """Koordynator i redaktor: 403 bez zmiany stanu i bez przycisku; superuser ma przycisk po lewej stronie."""
        for user in (self.coordinator, self.editor):
            with self.subTest(user=user.username):
                self.assertEqual(self.toggle(user).status_code, 403)
                self.text.refresh_from_db()
                self.assertFalse(self.text.take_locked)
                self.assertNotContains(self.detail(user), 'data-take-lock-toggle')
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse('core:toggle_text_take_lock', args=[self.text.pk])).status_code, 405)
        for label in ('Zablokuj tekst', 'Odblokuj tekst'):
            with self.subTest(label=label):
                actions = html.fromstring(self.detail(self.superuser).content).xpath(
                    '//div[contains(@class, "detail-header-actions")]/*')
                self.assertEqual(actions[0].get('action'), reverse('core:toggle_text_take_lock', args=[self.text.pk]))
                self.assertEqual(actions[0].xpath('normalize-space(.//button)'), label)
                self.assertEqual(actions[1].text_content().strip(), 'Edytuj w adminie')
                self.lock()

    def test_locked_text_rejects_every_take_path_except_superuser(self):
        """Pobranie „Do redakcji”, rezerwacja i przejęcie weryfikacji, rozpoczęcie przypisanego etapu (także przez
        koordynatora), rozpoczęcie W1, wznowienie redakcji i przypisanie rozdziału przez koordynatora: przy blokadzie
        odmowa z komunikatem i bez zmian, superuser może, po odblokowaniu znów działa."""
        def handed_over():
            self.begin_editing()
            claim_stage(self.text, 'first_verification', self.verifier_1)
            send_to_first_verification(self.text, self.editor, self.today)

        def second_verification():
            self.finish_first_verification()
            resume_editing(self.text, self.editor, self.today)
            send_to_second_verification(self.text, self.editor, self.today)

        def start_assigned(user):
            start_assigned_stage(user=user, stage_id=self.stage('first_verification').pk, started_at=self.today)

        book = Anthology.objects.create(title='Powieść z blokadą', is_novel=True)
        chapter = new_chapter(book, book.novel, chapter_number=1, length=100)

        def assign_chapter(user):
            with locked_book(book.pk, user, novel_token(book, user)) as locked:
                assign_chapters(locked, user, [chapter.pk], [('editor', self.editor)])

        cases = (
            ('pobranie „Do redakcji”', None, self.editor, self.text,
             lambda user: claim_ready_for_editing(self.text, user, self.today)),
            ('rezerwacja W1', self.begin_editing, self.verifier_1, self.text,
             lambda user: claim_stage(self.text, 'first_verification', user)),
            ('przejęcie W2', second_verification, self.verifier_2, self.text,
             lambda user: claim_stage(self.text, 'second_verification', user)),
            ('rozpoczęcie W1', handed_over, self.verifier_1, self.text,
             lambda user: start_reserved_first_verification(self.text, user, self.today)),
            ('rozpoczęcie przypisanego etapu przez koordynatora', handed_over, self.coordinator, self.text,
             start_assigned),
            ('wznowienie redakcji', self.finish_first_verification, self.editor, self.text,
             lambda user: resume_editing(self.text, user, self.today)),
            ('przypisanie rozdziału przez koordynatora', None, self.coordinator, chapter, assign_chapter),
        )
        for name, prepare, actor, text, act in cases:
            with self.subTest(name), transaction.atomic():
                if prepare:
                    prepare()
                toggle_take_lock(user=self.superuser, text_id=text.pk)
                before = self.work_state(), list(A.objects.filter(text=chapter).values_list('assigned_to_id'))
                with self.assertRaisesMessage(ValidationError, 'tymczasowo zablokowany'):
                    act(actor)
                self.assertEqual((self.work_state(), list(A.objects.filter(text=chapter).values_list('assigned_to_id'))),
                                 before)
                with transaction.atomic():
                    act(self.superuser)
                    transaction.set_rollback(True)
                toggle_take_lock(user=self.superuser, text_id=text.pk)
                act(actor)
                transaction.set_rollback(True)

    def test_locked_text_shows_note_instead_of_take_forms(self):
        """Lista „Teksty do wzięcia” i podgląd: zablokowany tekst widoczny z dopiskiem, bez przycisku „Przejmij”
        (superuser nadal go ma); informacja o blokadzie widoczna dla wszystkich; przejęcie przez adres – odmowa."""
        self.lock()
        pages = (reverse('core:available_texts'), reverse('core:assigned_text_detail', args=[self.text.pk]))
        for user, can_take in ((self.editor, False), (self.superuser, True)):
            self.client.force_login(user)
            for page in pages:
                with self.subTest(user=user.username, page=page):
                    response = self.client.get(page)
                    self.assertContains(response, self.text.title)
                    take_url = reverse('core:take_workflow_stage', args=[self.initial_stage.pk])
                    self.assertEqual(f'action="{take_url}"' in response.content.decode(), can_take)
                    self.assertEqual('data-take-locked' in response.content.decode(), not can_take)
        detail = self.detail(self.editor)
        self.assertContains(detail, 'Zablokowany')
        self.assertContains(detail, self.superuser.get_username())
        response = self.client.post(reverse('core:take_workflow_stage', args=[self.initial_stage.pk]),
                                    {'started_at': self.today.isoformat()}, follow=True)
        self.assertContains(response, TAKE_LOCKED_MESSAGE)
        self.assertFalse(A.objects.filter(text=self.text, assigned_to=self.editor).exists())

    def test_locked_text_hides_start_and_resume_buttons(self):
        """Przy blokadzie podgląd nie pokazuje „Rozpocznij etap” ani „Wznów redakcję” (superuser – tak)."""
        def handed_over():
            self.begin_editing()
            claim_stage(self.text, 'first_verification', self.verifier_1)
            send_to_first_verification(self.text, self.editor, self.today)

        cases = (('Rozpocznij etap', handed_over, self.verifier_1), ('Wznów redakcję', self.finish_first_verification,
                                                                    self.editor))
        for label, prepare, user in cases:
            with self.subTest(label), transaction.atomic():
                prepare()
                self.assertContains(self.detail(user), label)
                self.lock()
                self.assertNotContains(self.detail(user), label)
                self.assertContains(self.detail(self.superuser), label)
                transaction.set_rollback(True)

    def test_locked_text_freezes_booking_date_but_allows_cancel(self):
        """Rezerwacja z przyszłą datą sprzed blokady: każda zmiana daty (przyspieszenie, także na dziś, i przesunięcie
        na później) – odmowa bez zmian; superuser może (data dalej czeka na odblokowanie); odwołanie – dozwolone."""
        booking = claim_ready_for_editing(self.text, self.editor, self.today + timedelta(days=5))
        self.lock()
        for days in (0, 3, 7):
            with self.subTest(days=days):
                with self.assertRaisesMessage(ValidationError, TAKE_LOCKED_MESSAGE):
                    change_scheduled_stage(user=self.editor, stage_id=booking.pk,
                                           started_at=self.today + timedelta(days=days))
                booking.refresh_from_db()
                self.assertEqual((booking.is_held, booking.planned_start), (True, self.today + timedelta(days=5)))
        change_scheduled_stage(user=self.superuser, stage_id=booking.pk, started_at=self.today + timedelta(days=7))
        booking.refresh_from_db()
        self.assertEqual((booking.is_held, booking.planned_start), (True, self.today + timedelta(days=7)))
        detail = html.fromstring(self.detail(self.editor).content)
        self.assertTrue(detail.xpath('//*[@data-schedule-frozen]//button[@value="cancel"]'))
        self.assertFalse(detail.xpath('//input[@name="started_at"][@id="schedule-%d"]' % booking.pk))
        change_scheduled_stage(user=self.editor, stage_id=booking.pk, cancel=True)
        booking.refresh_from_db()
        self.assertEqual((booking.started_at, booking.held_started_at), (None, None))

    def test_lock_holds_future_booking_until_unlock(self):
        """Rezerwacja na za 5 dni + blokada: po minięciu daty etap się nie rozpoczyna (stan „Wstrzymany”, bez
        przekazania pracy, odmowa przy próbie); po odblokowaniu startuje w dniu odblokowania. Etap rozpoczęty przed
        blokadą zostaje bez zmian. Odblokowanie przed terminem przywraca pierwotną datę."""
        later = self.today + timedelta(days=6)
        booking = claim_ready_for_editing(self.text, self.editor, self.today + timedelta(days=5))
        claim_stage(self.text, 'first_verification', self.verifier_1)
        self.lock()
        with patch('django.utils.timezone.localdate', return_value=later):
            booking.refresh_from_db()
            self.assertFalse(stage_is_active(booking, later))
            response = self.detail(self.editor)
            self.assertContains(response, 'Wstrzymany (tekst zablokowany)')
            self.assertContains(response, (self.today + timedelta(days=5)).strftime('%d.%m.%Y'))
            self.assertNotContains(response, 'W trakcie')
            self.assertFalse(response.context['can_send_to_first_verification'])
            self.assertContains(response, 'Zaplanowane rozpoczęcia są wstrzymane')
            self.client.force_login(self.superuser)
            self.assertContains(self.client.get(reverse('core:person_detail', args=[self.editor.person_profile.pk])),
                                'Wstrzymany (tekst zablokowany)')
            with transaction.atomic(), self.assertRaisesMessage(ValidationError, TAKE_LOCKED_MESSAGE):
                send_to_first_verification(self.text, self.editor, later)
            self.lock()
            booking.refresh_from_db()
            self.assertEqual((booking.started_at, booking.held_started_at, booking.is_held), (later, None, False))
            self.assertTrue(stage_is_active(booking, later))
            response = self.detail(self.editor)
            self.assertNotContains(response, 'Wstrzymany (tekst zablokowany)')
            self.assertTrue(response.context['can_send_to_first_verification'])

        for name, starts_in, after_unlock in (('termin nie minął', 5, 5), ('rozpoczęty przed blokadą', 0, 0)):
            with self.subTest(name), transaction.atomic():
                S.objects.filter(pk=booking.pk).update(started_at=self.today + timedelta(days=starts_in))
                self.lock()
                booking.refresh_from_db()
                self.assertEqual(booking.is_held, starts_in > 0)
                self.lock()
                booking.refresh_from_db()
                self.assertEqual((booking.started_at, booking.held_started_at),
                                 (self.today + timedelta(days=after_unlock), None))
                transaction.set_rollback(True)
