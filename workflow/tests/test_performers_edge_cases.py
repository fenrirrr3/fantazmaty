"""Poprawianie wykonawców w adminie: przypisania z powtórzeń, przekazań i niespójnych powiązań,
puste wykonawstwo oraz formularz etapów na stronie tekstu."""
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from fantazmaty.testing import form_value
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from workflow.admin_performers import correct_stage_performers
from workflow.models import WorkflowRepetition, WorkflowRoleAssignment as A, WorkflowStage as S
from workflow.repetitions import repeat_stages

from .support import add_roles


class PerformerCorrectionGapTests(StatusAssignmentFixtures):
    """Korekta wykonawcy etapu przez correct_stage_performers."""

    def setUp(self):
        super().setUp()
        add_roles(self.member, 'Redaktor', 'Korektor', 'Weryfikator')
        add_roles(self.other, 'Redaktor', 'Korektor', 'Weryfikator')

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


class PerformerInlineGapTests(StatusAssignmentFixtures):
    """Tabela etapów na stronie tekstu w adminie."""

    def setUp(self):
        super().setUp()
        add_roles(self.member, 'Redaktor')
        self.editor = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.editing = S.objects.create(text=self.text, stage_type='editing', assignment=self.editor,
                                        started_at=self.today)
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
