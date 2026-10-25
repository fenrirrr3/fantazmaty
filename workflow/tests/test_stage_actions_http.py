"""Przyciski procesu na stronie tekstu (żądania HTTP): token stanu tekstu, komunikaty sukcesu i błędu,
przekierowania oraz brak zmian po odrzuconym żądaniu."""
from datetime import timedelta

from django.test import TestCase, tag
from django.urls import reverse
from lxml import html

from core.workflow_tokens import make_token
from fantazmaty.testing import edit_token
from texts.models import Text
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.repetitions import repeat_stages
from workflow.services import claim_stage, complete_stage, resume_editing

from .support import AnthologyWorkflowMixin


@tag('slow')
class StageActionEndpointTests(AnthologyWorkflowMixin, TestCase):
    """Tekst w antologii gotowy do redakcji; osoby z WorkflowTestDataMixin."""

    def post(self, user, name, *args, **data):
        self.client.force_login(user)
        response = self.client.post(reverse(name, args=args or [self.text.pk]), data)
        # Only messages added by this request (earlier ones stay queued until a page displays them).
        return response, [str(message) for message in response.wsgi_request._messages._queued_messages]

    def text_action(self, user, name, token=None):
        """Przycisk przejścia redakcji: formularz zawiera token aktualnego stanu tekstu."""
        response, messages = self.post(user, name, workflow_token=token or make_token(self.text, user))
        self.assertRedirects(response, reverse('core:assigned_text_detail', args=[self.text.pk]),
                             fetch_redirect_response=False)
        return messages

    def proofreading(self, started_at=None):
        self.initial_stage.delete()
        assignment = A.objects.create(text=self.text, role='proofreader_1', assigned_to=self.proofreader)
        return S.objects.create(text=self.text, stage_type='first_proofreading', assignment=assignment,
                                started_at=started_at)

    def test_editor_buttons_lead_text_through_both_verifications(self):
        self.begin_editing()
        self.assertEqual(self.text_action(self.editor, 'core:send_to_first_verification_stage'),
                         ['Przekazano tekst do pierwszej weryfikacji.'])
        first = claim_stage(self.text, 'first_verification', self.verifier_1, self.today)
        response, messages = self.post(self.verifier_1, 'core:complete_workflow_stage', first.pk)
        self.assertEqual(messages, ['Zakończono etap.'])
        first.refresh_from_db()
        self.assertEqual(first.ended_at, self.today)
        self.assertEqual(self.text_action(self.editor, 'core:resume_text_editing'),
                         ['Tekst wrócił do redaktora. Wznowiono redakcję.'])
        self.assertEqual(self.text_action(self.editor, 'core:send_text_to_author_stage'),
                         ['Przekazano tekst do pracy autora.'])
        resume_editing(self.text, self.editor, self.today)
        self.assertEqual(self.text_action(self.editor, 'core:send_to_second_verification_stage'),
                         ['Przekazano tekst do drugiej weryfikacji.'])
        second = claim_stage(self.text, 'second_verification', self.verifier_2, self.today)
        complete_stage(second, self.verifier_2, self.today)
        resume_editing(self.text, self.editor, self.today)
        self.assertEqual(self.text_action(self.editor, 'core:finish_text_editing'),
                         ['Zakończono redakcję. Przekazano tekst do kontroli koordynatora redakcji.'])
        self.assertIsNone(self.stage('editing_control').started_at)

    def test_stale_token_and_foreign_text_are_refused_with_a_message(self):
        editing = self.begin_editing()
        messages = self.text_action(self.editor, 'core:send_to_first_verification_stage', token='nieaktualny')
        self.assertIn('Odśwież podgląd', messages[0])
        old_token = make_token(self.text, self.editor)
        claim_stage(self.text, 'first_verification', self.verifier_1)
        messages = self.text_action(self.editor, 'core:send_to_first_verification_stage', token=old_token)
        self.assertIn('Odśwież podgląd', messages[0])
        messages = self.text_action(self.other_editor, 'core:send_to_first_verification_stage')
        self.assertEqual(len(messages), 1)
        self.assertNotIn('Przekazano', messages[0])
        editing.refresh_from_db()
        self.assertFalse(editing.is_completed)

    def test_assigned_person_starts_and_reschedules_own_booking(self):
        stage = self.proofreading()
        _, messages = self.post(self.proofreader, 'core:start_assigned_workflow_stage', stage.pk, started_at='')
        self.assertEqual(len(messages), 1)
        stage.refresh_from_db()
        self.assertIsNone(stage.started_at)
        _, messages = self.post(self.other_proofreader, 'core:start_assigned_workflow_stage', stage.pk,
                                started_at=self.today.isoformat())
        self.assertNotEqual(messages, ['Zapisano datę rozpoczęcia etapu.'])
        booked = self.today + timedelta(days=3)
        _, messages = self.post(self.proofreader, 'core:start_assigned_workflow_stage', stage.pk,
                                started_at=booked.isoformat())
        self.assertEqual(messages, ['Zapisano datę rozpoczęcia etapu.'])
        moved = self.today + timedelta(days=5)
        for value, expected in (('jutro', None), (moved.isoformat(), 'Zmieniono datę rozpoczęcia.')):
            with self.subTest(value=value):
                _, messages = self.post(self.proofreader, 'core:change_scheduled_workflow_stage', stage.pk,
                                        started_at=value)
                if expected:
                    self.assertEqual(messages, [expected])
                else:
                    self.assertEqual(len(messages), 1)
        stage.refresh_from_db()
        self.assertEqual(stage.started_at, moved)
        _, messages = self.post(self.proofreader, 'core:change_scheduled_workflow_stage', stage.pk, action='cancel')
        self.assertEqual(messages, ['Cofnięto przydział – etap może przejąć inna osoba.'])

    def test_completion_form_errors_leave_stage_open(self):
        stage = self.proofreading(started_at=self.today)
        for user, data in ((self.proofreader, {'ended_at': 'wczoraj'}),
                           (self.proofreader, {'ended_at': (self.today + timedelta(days=1)).isoformat()}),
                           (self.other_proofreader, {})):
            with self.subTest(user=user.username, data=data):
                response, messages = self.post(user, 'core:complete_workflow_stage', stage.pk, **data)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(len(messages), 1)
                self.assertNotEqual(messages, ['Zakończono etap.'])
                stage.refresh_from_db()
                self.assertFalse(stage.is_completed)

    def test_coordinator_withdraws_and_superuser_restores_text(self):
        self.begin_editing()
        response, _ = self.post(self.editor, 'core:withdraw_text', workflow_token=make_token(self.text, self.editor))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.text_action(self.coordinator, 'core:withdraw_text'), ['Tekst jest wycofany z procesu.'])
        self.assertTrue(S.objects.current_cycle().filter(text=self.text, stage_type='withdrawn').exists())
        # A repeated request is idempotent: no second withdrawal record.
        self.text_action(self.coordinator, 'core:withdraw_text')
        self.assertEqual(S.objects.filter(text=self.text, stage_type='withdrawn').count(), 1)
        self.assertIn('Odśwież podgląd', self.text_action(self.superuser, 'core:restore_withdrawn_text', token='x')[0])
        self.assertEqual(self.text_action(self.superuser, 'core:restore_withdrawn_text'),
                         ['Przywrócono tekst do procesu – praca wraca do stanu sprzed wycofania.'])
        self.assertTrue(self.stage('editing').started_at)

    def test_superuser_cancels_unstarted_repetition_and_restores_ready_text(self):
        editing = self.begin_editing()
        S.objects.filter(pk=editing.pk).update(ended_at=self.today, is_completed=True)
        self.stage('first_verification').delete()
        ready = S.objects.create(text=self.text, stage_type='ready', started_at=self.today)
        run = repeat_stages(self.text, ['editing'], self.superuser)
        _, messages = self.post(self.superuser, 'core:cancel_workflow_repetition', self.text.pk, run.pk,
                                _edit_version=edit_token(self.superuser, self.text))
        self.assertEqual(messages, ['Anulowano powtórzenie. Przywrócono poprzedni stan tekstu.'])
        run.refresh_from_db()
        ready.refresh_from_db()
        self.assertIsNotNone(run.canceled_at)
        self.assertTrue(ready.is_current)
        self.assertFalse(run.stages.filter(is_current=True).exists())
        return run

    def test_second_cancellation_message_is_plain_text(self):
        """Regresja: komunikat błędu to sam tekst, bez „['…']” z repr listy."""
        run = self.test_superuser_cancels_unstarted_repetition_and_restores_ready_text()
        _, messages = self.post(self.superuser, 'core:cancel_workflow_repetition', self.text.pk, run.pk,
                                _edit_version=edit_token(self.superuser, self.text))
        self.assertEqual(messages, ['Ta kolejka jest już zamknięta.'])

    def test_restart_preview_rejects_invalid_choice_and_expired_token(self):
        editing = self.begin_editing()
        S.objects.filter(pk=editing.pk).update(ended_at=self.today, is_completed=True)
        _, messages = self.post(self.superuser, 'core:restart_text_workflow')
        self.assertEqual(len(messages), 1)
        _, messages = self.post(self.superuser, 'core:restart_text_workflow', stages=['editing'],
                                performer_editor=self.other_editor.pk, confirm_restart='yes', token='wygasły')
        self.assertEqual(messages, ['Podgląd wygasł. Przygotuj go ponownie.'])
        self.assertFalse(self.text.repetitions.exists())

    def test_skip_and_handoff_report_their_outcome(self):
        self.initial_stage.delete()
        fourth = S.objects.create(text=self.text, stage_type='fourth_proofreading')
        _, messages = self.post(self.superuser, 'core:skip_workflow_stage', fourth.pk)
        self.assertEqual(messages, ['Pominięto czwartą korektę. Udostępniono kolejny etap.'])
        styling = self.stage('styling')
        assignment = A.objects.create(text=self.text, role='styling', assigned_to=self.superuser)
        S.objects.filter(pk=styling.pk).update(assignment=assignment, started_at=self.today)
        self.client.force_login(self.superuser)
        url = reverse('core:handoff_workflow_stage', args=[styling.pk])
        token = html.fromstring(self.client.get(url).content).xpath('//input[@name="_edit_version"]/@value')[0]
        response = self.client.post(url, {'assigned_to': self.superuser.pk, 'expected_assignment_id': assignment.pk,
                                          '_edit_version': token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertEqual(self.client.put(url).status_code, 405)

    def test_rejected_requests_explain_why_and_change_nothing(self):
        editing = self.begin_editing()
        first = self.stage('first_verification')
        refused = (
            # Editing has its own transitions; the generic "finish" is refused by the service.
            (self.editor, 'core:complete_workflow_stage', (editing.pk,), {}),
            (self.editor, 'core:start_assigned_workflow_stage', (editing.pk,), {'started_at': self.today.isoformat()}),
            (self.editor, 'core:change_scheduled_workflow_stage', (editing.pk,), {'started_at': self.today.isoformat()}),
            (self.verifier_1, 'core:take_workflow_stage', (first.pk,), {'started_at': 'pojutrze'}),
            (self.proofreader, 'core:take_workflow_stage', (first.pk,), {}),
            (self.coordinator, 'core:withdraw_text', (self.text.pk,), {'workflow_token': 'stary'}),
        )
        for user, name, args, data in refused:
            with self.subTest(name=name, user=user.username):
                response, messages = self.post(user, name, *args, **data)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(len(messages), 1)
                self.assertEqual(response.wsgi_request._messages._queued_messages[0].level_tag, 'error')
        editing.refresh_from_db()
        first.refresh_from_db()
        self.assertFalse(editing.is_completed)
        self.assertIsNone(first.assignment_id)
        self.assertFalse(S.objects.filter(text=self.text, stage_type='withdrawn').exists())
        # A started stage is no longer offered, even when the request names it directly.
        _, messages = self.post(self.other_editor, 'core:take_workflow_stage', editing.pk,
                                started_at=self.today.isoformat())
        self.assertEqual(messages, ['Ten etap nie jest już dostępny do przejęcia.'])

    def test_admin_actions_report_service_refusals(self):
        editing = self.begin_editing()
        S.objects.filter(pk=editing.pk).update(ended_at=self.today, is_completed=True)
        self.client.force_login(self.superuser)
        url = reverse('core:restart_text_workflow', args=[self.text.pk])
        data = {'stages': ['editing'], 'performer_editor': self.other_editor.pk}
        token = self.client.post(url, data).context['token']
        S.objects.create(text=self.text, stage_type='ready', started_at=self.today)  # state changed after preview
        _, messages = self.post(self.superuser, 'core:restart_text_workflow', token=token, confirm_restart='yes', **data)
        self.assertEqual(messages, ['Stan tekstu zmienił się. Przygotuj podgląd ponownie.'])
        self.assertFalse(self.text.repetitions.exists())
        fourth = S.objects.create(text=Text.objects.create(title='Inny', length=1), stage_type='fourth_proofreading')
        A.objects.create(text=fourth.text, role='proofreader_4', assigned_to=self.proofreading_coordinator)
        _, messages = self.post(self.superuser, 'core:skip_workflow_stage', fourth.pk)
        self.assertEqual(len(messages), 1)
        fourth.refresh_from_db()
        self.assertFalse(fourth.is_skipped)
