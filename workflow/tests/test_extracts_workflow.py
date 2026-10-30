"""Skrócony workflow Ekstraktów: Do redakcji → Redakcja → Pierwsza weryfikacja → Pierwsza korekta → Gotowe.

Wyłączone etapy nie są oferowane w karcie tekstu, „Teksty do wzięcia” ani w adminie, serwer je odrzuca,
a zaległe (sprzed zmiany) prowadzą po zakończeniu do najbliższego dozwolonego etapu.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase, tag
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.forms import CompleteStageForm, RestartWorkflowForm
from core.supervision import integrity_issues
from core.workflow_tokens import make_token
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from texts.models import Anthology, Text
from workflow.admin_add_stage import add_missing_stage
from workflow.admin_stage_dates import StageDatesForm, set_stage_dates
from workflow.admin_stage_edit import edit_stage
from workflow.admin_status import set_admin_status
from workflow.availability import claim_access, claim_reason
from workflow.catalog import EXTRACTS_STAGE_TYPES
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.read_queries import available_stages
from workflow.repetitions import repeat_stages, validate_repeat
from workflow.services import (STAGE_ROLES, claim_ready_for_editing, claim_stage, complete_stage,
                               current_stage_queryset, finish_editing_to_coordinator, resume_editing,
                               send_text_to_author, send_to_first_proofreading, send_to_first_verification,
                               send_to_second_verification, skip_fourth_proofreading)

DISABLED = ('author_editing', 'second_verification', 'editing_control', 'second_proofreading',
            'third_verification', 'coordinator_control', 'editor_control', 'third_proofreading',
            'fourth_proofreading', 'styling')


@tag('slow')
class ExtractsWorkflowTests(WorkflowTestDataMixin, TestCase):
    """Wspólny tekst tomu „Ekstrakty 9” w stanie „Do redakcji”; osoby z WorkflowTestDataMixin."""

    def setUp(self):
        super().setUp()
        self.book = Anthology.objects.create(title='Ekstrakty 9', is_extracts=True)
        self.text = self.book.texts.get()
        self.initial_stage = self.text.workflow_stages.get()
        self.users = {'editor': self.editor, 'verifier_1': self.verifier_1, 'verifier_2': self.verifier_2,
                      'proofreader_1': self.proofreader, 'proofreader_2': self.other_proofreader}

    # --- pomocniki ---------------------------------------------------------------------------------

    def post(self, user, name, *args, **data):
        self.client.force_login(user)
        response = self.client.post(reverse(name, args=args or [self.text.pk]), data)
        return [str(message) for message in response.wsgi_request._messages._queued_messages]

    def text_action(self, user, name, text=None):
        text = text or self.text
        text.refresh_from_db()
        return self.post(user, name, text.pk, workflow_token=make_token(text, user))

    def current_kinds(self, text=None):
        return sorted(current_stage_queryset(text or self.text).values_list('stage_type', flat=True))

    def open_kinds(self, text=None):
        return sorted(current_stage_queryset(text or self.text).filter(is_completed=False)
                      .values_list('stage_type', flat=True))

    def legacy(self, *rows, text=None):
        """Stan sprzed zmiany: zastępuje etapy wierszami (rodzaj, 'done'|'active'|'pending')."""
        text = text or self.text
        text.workflow_stages.all().delete()
        day = self.today - timedelta(days=len(rows) + 1)
        for kind, state in rows:
            role = STAGE_ROLES.get(kind)
            if role and state != 'pending' and not A.objects.filter(text=text, role=role, is_current=True).exists():
                A.objects.create(text=text, role=role, assigned_to=self.users.get(role, self.superuser))
            stage = S.objects.create(text=text, stage_type=kind, started_at=day if state != 'pending' else None,
                                     ended_at=day if state == 'done' else None, is_completed=state == 'done')
            if state == 'done' and role:
                S.objects.filter(pk=stage.pk).update(
                    assignment=A.objects.get(text=text, role=role, is_current=True))
            day += timedelta(days=1)
        return current_stage_queryset(text).filter(is_completed=False).order_by('-pk').first()

    def detail_actions(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        tree = html.fromstring(response.content)
        return response, set(tree.xpath('//form/@action')), tree

    # --- pełna ścieżka -----------------------------------------------------------------------------

    def test_full_path_from_ready_for_editing_to_ready_through_pages(self):
        """Przyciski karty tekstu prowadzą Ekstrakty przez Redakcję, W1 (z rezerwacją) i K1 do „Gotowe”."""
        today = self.today.isoformat()
        self.post(self.editor, 'core:take_workflow_stage', self.initial_stage.pk, started_at=today)
        self.assertEqual(self.open_kinds(), ['editing', 'first_verification'])
        first = current_stage_queryset(self.text).get(stage_type='first_verification')
        self.assertEqual(self.post(self.verifier_1, 'core:take_workflow_stage', first.pk),
                         ['Zarezerwowano pierwszą weryfikację. Zaczekaj na przekazanie tekstu przez redaktora.'])
        _, actions, _ = self.detail_actions(self.editor)
        self.assertIn(reverse('core:send_to_first_verification_stage', args=[self.text.pk]),
                      actions)
        self.assertEqual(self.text_action(self.editor, 'core:send_to_first_verification_stage'),
                         ['Przekazano tekst do pierwszej weryfikacji.'])
        self.post(self.verifier_1, 'core:start_assigned_workflow_stage', first.pk, started_at=today)
        self.assertEqual(self.post(self.verifier_1, 'core:complete_workflow_stage', first.pk, ended_at=today),
                         ['Zakończono etap.'])
        self.assertEqual(self.open_kinds(), ['first_proofreading'])
        proofreading = current_stage_queryset(self.text).get(stage_type='first_proofreading')
        self.post(self.proofreader, 'core:take_workflow_stage', proofreading.pk, started_at=today)
        self.assertEqual(self.post(self.proofreader, 'core:complete_workflow_stage', proofreading.pk,
                                   ended_at=today), ['Zakończono etap.'])
        self.assertEqual(list(S.objects.filter(text=self.text).order_by('pk').values_list('stage_type', flat=True)),
                         ['editing', 'first_verification', 'first_proofreading', 'ready'])
        self.assertTrue(current_stage_queryset(self.text).get(stage_type='ready').started_at)
        self.assertFalse([item for item in integrity_issues() if self.text.title in str(item)])

    # --- wyłączone przejścia redakcji --------------------------------------------------------------

    def test_editing_after_first_verification_offers_only_first_proofreading(self):
        """Redakcja po W1 (stan zaległy): karta pokazuje tylko „Przekaż do pierwszej korekty”, przycisk działa."""
        self.legacy(('first_verification', 'done'), ('editing', 'active'))
        response, actions, _ = self.detail_actions(self.editor)
        self.assertContains(response, 'Przekaż do pierwszej korekty')
        for name in ('send_text_to_author_stage', 'send_to_second_verification_stage',
                     'send_to_first_verification_stage', 'resume_text_editing'):
            with self.subTest(przycisk=name):
                self.assertNotIn(reverse('core:' + name, args=[self.text.pk]), actions)
        self.assertNotContains(response, 'Przekaż redakcję do kontroli')
        self.assertEqual(self.text_action(self.editor, 'core:finish_text_editing'),
                         ['Zakończono redakcję. Przekazano tekst do pierwszej korekty.'])
        self.assertEqual(self.open_kinds(), ['first_proofreading'])

    def test_server_rejects_every_disabled_editing_transition(self):
        """Każde przejście redakcji do wyłączonego etapu – w serwisie i przez stronę – kończy się odmową bez zmian."""
        self.legacy(('first_verification', 'done'), ('editing', 'active'))
        before = self.current_kinds()
        for service in (send_text_to_author, send_to_second_verification, finish_editing_to_coordinator):
            with self.subTest(serwis=service.__name__):
                with self.assertRaisesMessage(ValidationError, 'jest wyłączony w Ekstraktach'):
                    service(self.text, self.editor, self.today)
        for name in ('send_text_to_author_stage', 'send_to_second_verification_stage'):
            with self.subTest(widok=name):
                messages = self.text_action(self.editor, 'core:' + name)
                self.assertEqual(len(messages), 1)
                self.assertIn('jest wyłączony w Ekstraktach', messages[0])
        with self.assertRaisesMessage(ValidationError, 'Pierwsza weryfikacja jest już zakończona.'):
            send_to_first_verification(self.text, self.editor, self.today)
        self.assertEqual(self.current_kinds(), before)

    def test_first_proofreading_shortcut_requires_extracts_and_first_verification(self):
        """Skrót redakcja → K1 działa tylko w Ekstraktach i dopiero po pierwszej weryfikacji."""
        claim_ready_for_editing(self.text, self.editor, self.today)
        with self.assertRaisesMessage(ValidationError, 'Najpierw przekaż tekst do pierwszej weryfikacji.'):
            send_to_first_proofreading(self.text, self.editor, self.today)
        ordinary = Text.objects.create(title='Zwykły', length=100, anthology=Anthology.objects.create(title='Zwykła'))
        S.objects.create(text=ordinary, stage_type='ready_for_editing')
        claim_ready_for_editing(ordinary, self.editor, self.today)
        with self.assertRaisesMessage(ValidationError, 'dotyczy tylko Ekstraktów'):
            send_to_first_proofreading(ordinary, self.editor, self.today)

    def test_first_proofreading_shortcut_needs_active_editing_and_no_other_open_stages(self):
        """Po W1 skrót do K1 wymaga trwającej redakcji; inny otwarty etap redakcyjny (zaległa W2) blokuje go."""
        cases = (
            ('brak aktywnej redakcji', (('first_verification', 'done'), ('editing', 'done')),
             'Nie ma aktywnej redakcji do zakończenia.'),
            ('otwarta druga weryfikacja', (('first_verification', 'done'), ('editing', 'active'),
                                          ('second_verification', 'active')),
             'Najpierw zakończ pozostałe otwarte etapy redakcyjne.'),
        )
        for name, rows, message in cases:
            with self.subTest(name):
                self.legacy(*rows)
                before = self.open_kinds()
                with self.assertRaisesMessage(ValidationError, message):
                    send_to_first_proofreading(self.text, self.editor, self.today)
                self.assertEqual(self.open_kinds(), before)

    # --- zaległe wyłączone etapy -------------------------------------------------------------------

    def test_completing_disabled_stage_leads_to_nearest_allowed_stage(self):
        """Zakończenie zaległego wyłączonego etapu tworzy najbliższy dozwolony etap (W1, K1 albo Gotowe)."""
        cases = [
            ((('editing', 'done'), ('author_editing', 'active')), 'first_verification'),
            ((('first_verification', 'done'), ('author_editing', 'active')), 'first_proofreading'),
            ((('first_verification', 'done'), ('second_verification', 'active')), 'first_proofreading'),
            ((('first_verification', 'done'), ('editing_control', 'active')), 'first_proofreading'),
            *[((('first_proofreading', 'done'), (kind, 'active')), 'ready')
              for kind in ('second_proofreading', 'third_verification', 'coordinator_control', 'editor_control',
                           'third_proofreading', 'fourth_proofreading', 'styling')],
        ]
        for rows, expected in cases:
            with self.subTest(etap=rows[-1][0], poprzedni=rows[0][0]):
                stage = self.legacy(*rows)
                complete_stage(stage, self.superuser, self.today)
                self.assertEqual(self.open_kinds(), [expected])

    def test_author_stage_closes_by_complete_button_not_by_resuming_editing(self):
        """Zaległy „Plik u autora”: karta oferuje „Zakończ etap”, wznowienie redakcji jest odrzucane."""
        stage = self.legacy(('first_verification', 'done'), ('editing', 'done'), ('author_editing', 'active'))
        response, actions, _ = self.detail_actions(self.editor)
        self.assertIn(reverse('core:complete_workflow_stage', args=[stage.pk]), actions)
        self.assertNotIn(reverse('core:resume_text_editing', args=[self.text.pk]), actions)
        with self.assertRaisesMessage(ValidationError, 'etap „Plik u autora” jest wyłączony'):
            resume_editing(self.text, self.editor, self.today)
        self.assertEqual(self.post(self.editor, 'core:complete_workflow_stage', stage.pk,
                                   ended_at=self.today.isoformat()), ['Zakończono etap.'])
        self.assertEqual(self.open_kinds(), ['first_proofreading'])

    def test_editing_control_has_no_return_to_editing(self):
        """Zaległa kontrola K. redakcji: bez wyboru „dalsza redakcja”; odmowa powrotu, zakończenie → K1."""
        stage = self.legacy(('first_verification', 'done'), ('editing', 'done'), ('editing_control', 'active'))
        self.assertNotIn('send_to_proofreading', CompleteStageForm(stage=stage).fields)
        response, _, _ = self.detail_actions(self.superuser)
        self.assertNotContains(response, 'Potrzebna dalsza redakcja')
        with self.assertRaisesMessage(ValidationError, 'nie ma powrotu do dalszej redakcji'):
            complete_stage(stage, self.superuser, self.today, send_to_proofreading=False)
        complete_stage(stage, self.superuser, self.today)
        stage.refresh_from_db()
        self.assertTrue(stage.send_to_proofreading)
        self.assertEqual(self.open_kinds(), ['first_proofreading'])

    def test_skipping_fourth_proofreading_finishes_extracts(self):
        """Pominięcie zaległej czwartej korekty w Ekstraktach kończy tekst etapem „Gotowe”."""
        stage = self.legacy(('first_proofreading', 'done'), ('fourth_proofreading', 'pending'))
        skip_fourth_proofreading(stage, self.superuser)
        self.assertEqual(self.open_kinds(), ['ready'])
        self.assertTrue(current_stage_queryset(self.text).get(stage_type='ready').started_at)

    def test_pending_disabled_stage_cannot_be_claimed(self):
        """Nieprzypisany zaległy wyłączony etap nie jest w „Teksty do wzięcia”, a przejęcie jest odrzucane."""
        stage = self.legacy(('first_verification', 'done'), ('editing', 'done'), ('second_verification', 'pending'))
        self.assertFalse(available_stages(self.verifier_2, claim_access(self.verifier_2)).filter(pk=stage.pk).exists())
        self.assertIn('jest wyłączony w Ekstraktach', claim_reason(
            stage, self.verifier_2, list(current_stage_queryset(self.text)), []))
        with self.assertRaisesMessage(ValidationError, 'jest wyłączony w Ekstraktach'):
            claim_stage(self.text, 'second_verification', self.verifier_2, self.today)
        messages = self.post(self.verifier_2, 'core:take_workflow_stage', stage.pk,
                             started_at=self.today.isoformat())
        self.assertIn('jest wyłączony w Ekstraktach', messages[0])
        self.assertFalse(A.objects.filter(text=self.text, role='verifier_2').exists())

    # --- admin i powtórzenia -----------------------------------------------------------------------

    def test_admin_forms_offer_only_extracts_stages(self):
        """Formularze admina (status, brakujący etap, korekta wykonania, powtórzenie) nie oferują wyłączonych etapów."""
        stage = self.legacy(('first_verification', 'done'), ('editing', 'active'))
        self.client.force_login(self.superuser)
        pages = {
            'status': (reverse('admin:texts_text_manual_status', args=[self.text.pk]), 'stage'),
            'brakujący etap': (reverse('admin:texts_text_add_stage', args=[self.text.pk]), 'kind'),
            'korekta wykonania': (reverse('admin:workflow_stage_correct', args=[stage.pk]), 'replacement'),
            'daty i przekazanie': (reverse('admin:workflow_stage_dates', args=[stage.pk]), 'next_stage'),
        }
        for label, (url, field) in pages.items():
            with self.subTest(formularz=label):
                tree = html.fromstring(self.client.get(url).content)
                offered = set(tree.xpath('//select[@name=$name]/option/@value', name=field)) - {''}
                self.assertTrue(offered)
                self.assertTrue(offered <= set(EXTRACTS_STAGE_TYPES), offered)
        self.assertEqual([kind for kind, _ in StageDatesForm(stage=stage).fields['next_stage'].choices if kind],
                         ['first_proofreading'])
        self.assertEqual([kind for kind, _ in RestartWorkflowForm(text=self.text).fields['stages'].choices],
                         ['editing', 'first_verification'])

    def test_admin_services_reject_disabled_stages(self):
        """Serwisy admina odrzucają wyłączony etap komunikatem po polsku i niczego nie zmieniają."""
        self.legacy(('first_verification', 'done'), ('editing', 'done'))
        attempts = {
            'status': lambda kind: set_admin_status(self.text.pk, kind, self.superuser, version_of(self.text)),
            'brakujący etap': lambda kind: add_missing_stage(self.text.pk, self.superuser, version_of(self.text),
                                                             kind=kind),
            'dodatkowe wykonanie': lambda kind: add_missing_stage(self.text.pk, self.superuser, version_of(self.text),
                                                                  kind=kind, performer=self.superuser, additional=True),
            'powtórzenie': lambda kind: validate_repeat(self.text, [kind]),
        }
        before = self.current_kinds()
        for label, attempt in attempts.items():
            for kind in ('second_verification', 'editing_control', 'styling'):
                with self.subTest(formularz=label, etap=kind):
                    with self.assertRaises(ValidationError) as caught:
                        attempt(kind)
                    self.assertIn('jest wyłączony w Ekstraktach', ' '.join(caught.exception.messages))
        self.assertEqual(self.current_kinds(), before)

    def test_completed_disabled_stage_cannot_be_reopened(self):
        """Cofnięcie zakończenia zaległego wyłączonego etapu: brak opcji w adminie i odmowa serwera."""
        self.legacy(('first_verification', 'done'), ('second_verification', 'done'), ('first_proofreading', 'pending'))
        second = current_stage_queryset(self.text).get(stage_type='second_verification')
        self.client.force_login(self.superuser)
        tree = html.fromstring(self.client.get(reverse('admin:workflow_stage_correct', args=[second.pk])).content)
        self.assertNotIn('reopen', tree.xpath('//select[@name="action"]/option/@value'))
        page = self.client.get(reverse('admin:texts_text_change', args=[self.text.pk])).content.decode()
        reopen = reverse('admin:workflow_stage_correct', args=[second.pk]) + '?action=reopen'
        self.assertNotIn(reopen, page)
        with self.assertRaisesMessage(ValidationError, 'jest wyłączony w Ekstraktach'):
            edit_stage(second.pk, self.superuser, version_of(self.text), action='reopen')
        second.refresh_from_db()
        self.assertTrue(second.is_completed)

    def test_raw_stage_admin_cannot_add_or_retype_stages(self):
        """Surowy admin etapów nie pozwala dodać etapu ani zmienić jego rodzaju."""
        stage = self.legacy(('first_verification', 'done'), ('editing', 'active'))
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse('admin:workflow_workflowstage_add')).status_code, 403)
        tree = html.fromstring(self.client.get(reverse('admin:workflow_workflowstage_change', args=[stage.pk])).content)
        self.assertFalse(tree.xpath('//*[@name="stage_type"]'))

    def test_admin_dates_form_finishes_editing_to_first_proofreading(self):
        """Admin „Daty i przekazanie”: redakcja po W1 → K1; wyłączony wybór jest odrzucony."""
        stage = self.legacy(('first_verification', 'done'), ('editing', 'active'))
        with self.assertRaisesMessage(ValidationError, 'jest wyłączony w Ekstraktach'):
            set_stage_dates(stage.pk, self.superuser, version_of(self.text), started_at=stage.started_at,
                            ended_at=self.today, finish=True, next_stage='author_editing')
        set_stage_dates(stage.pk, self.superuser, version_of(self.text), started_at=stage.started_at,
                        ended_at=self.today, finish=True, next_stage='first_proofreading')
        self.assertEqual(self.open_kinds(), ['first_proofreading'])

    def test_repetition_of_editing_and_first_verification_continues_to_proofreading(self):
        """Powtórzenie redakcji i W1 w Ekstraktach: kolejka bez powrotu do redakcji, potem pierwsza korekta."""
        self.legacy(('editing', 'done'), ('first_verification', 'done'), ('first_proofreading', 'active'))
        run = repeat_stages(self.text, ['editing', 'first_verification'], self.superuser,
                            assignees={'editor': self.other_editor, 'verifier_1': self.verifier_2})
        queue = list(run.stages.order_by('queue_position'))
        self.assertEqual([stage.stage_type for stage in queue], ['editing', 'first_verification'])
        for stage in queue:
            stage.refresh_from_db()
            claim_stage(self.text, stage.stage_type, stage.assignment.assigned_to, self.today, stage_id=stage.pk)
            complete_stage(stage, self.superuser, self.today)
        self.assertEqual(self.open_kinds(), ['first_proofreading'])


class OrdinaryAnthologyUnchangedTests(WorkflowTestDataMixin, TestCase):
    """Zwykła antologia zachowuje pełny workflow."""

    def test_ordinary_text_keeps_full_choices_and_transitions(self):
        """W zwykłej antologii admin oferuje wszystkie etapy, a W1 wraca do redakcji, nie do korekty."""
        self.text.anthology = Anthology.objects.create(title='Zwykła antologia')
        self.text.save()
        self.finish_first_verification()
        self.assertEqual(sorted(current_stage_queryset(self.text).filter(is_completed=False)
                                .values_list('stage_type', flat=True)), ['editing'])
        self.client.force_login(self.superuser)
        tree = html.fromstring(self.client.get(reverse('admin:texts_text_manual_status', args=[self.text.pk])).content)
        offered = set(tree.xpath('//select[@name="stage"]/option/@value'))
        self.assertTrue(set(DISABLED) <= offered)
