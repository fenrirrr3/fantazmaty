import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.db import IntegrityError, transaction
from django.http import QueryDict
from django.test import TestCase
from django.utils import timezone
from core.tests import CoreTestDataMixin
from texts.models import Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.completed_import import import_completed_workflow
from workflow.read_queries import annotate_my_work, dashboard_querysets
from core.views.people import _profile_assignments
from core.selectors.texts import text_detail_context
from core.selectors.reports import workflow_inactivity_context, workflow_activity_context
from core.supervision import integrity_issues
from workflow.repetitions import repeat_stages
from workflow.services import claim_stage, complete_stage


class CompletedImportTests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.text = Text.objects.create(title='Import bez dat', length=100, anthology=self.anthology)
        self.text.authors.add(self.author)
        self.editor.email='editor21@example.com';self.editor.save(update_fields=['email'])
        self.rows = [{'stage_type':'editing', 'assigned_to':self.editor.email}]

    def run_import(self, next_stage='ready', rows=None):
        return import_completed_workflow(text_id=self.text.pk, stages=self.rows if rows is None else rows, next_stage=next_stage)

    def test_completed_without_dates_is_not_workload(self):
        self.assertTrue(self.run_import())
        stage=S.objects.get(text=self.text,stage_type='editing')
        self.assertTrue(stage.is_completed); self.assertTrue(stage.imported_completed)
        self.assertIsNone(stage.started_at);self.assertIsNone(stage.ended_at)
        self.assertIsNone(stage.assignment.assigned_at)
        counts=annotate_my_work(Text.objects.filter(pk=self.text.pk),self.editor,timezone.localdate()).get()
        self.assertTrue(counts.work_completed);self.assertFalse(counts.work_active);self.assertFalse(counts.work_waiting)
        rows,summary=_profile_assignments(self.editor.person_profile,include_authors=False)
        self.assertTrue(next(r for r in rows if r['text']['pk']==self.text.pk)['has_completed_work'])
        self.assertFalse(any('Import bez dat' in i['detail'] for i in integrity_issues()))
        context=text_detail_context(user=self.superuser,text=self.text)
        self.assertTrue(context['is_ready'])
        from core.selectors.texts import _annotated_texts
        self.assertEqual(_annotated_texts().get(pk=self.text.pk).current_stage_type, 'ready')
        self.assertFalse(self.run_import())
        self.assertEqual(S.objects.filter(text=self.text).count(),2)

    def test_partial_import_does_not_mark_text_ready_or_reserve_editor(self):
        self.run_import('first_proofreading')
        state=annotate_my_work(Text.objects.filter(pk=self.text.pk),self.editor,timezone.localdate()).get()
        self.assertTrue(state.work_completed);self.assertFalse(state.work_waiting)
        pending=S.objects.get(text=self.text,stage_type='first_proofreading')
        self.assertFalse(pending.is_completed);self.assertFalse(pending.imported_completed)
        self.assertIsNone(pending.assignment_id)
        self.assertFalse(text_detail_context(user=self.superuser,text=self.text)['is_ready'])
        self.assertFalse(self.run_import('first_proofreading'))

    def test_new_work_requires_dates_and_cannot_opt_into_import(self):
        with self.assertRaises(ValidationError):
            S(text=self.text,stage_type='editing',is_completed=True).full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            S.objects.create(text=self.text,stage_type='editing',is_completed=True)
        with self.assertRaises(ValidationError):
            S.objects.create(text=self.text,stage_type='editing',is_completed=True,imported_completed=True)
        self.run_import()
        stage=S.objects.get(text=self.text,stage_type='editing')
        with self.assertRaises(IntegrityError),transaction.atomic():
            S.objects.filter(pk=stage.pk).update(is_completed=False)

    def test_repeat_imported_work_has_normal_date_requirements(self):
        self.run_import()
        run=repeat_stages(self.text,['editing'],self.superuser)
        stage=run.stages.get()
        self.assertFalse(stage.imported_completed)
        self.assertIsNone(stage.assignment.assigned_to_id)
        current=claim_stage(self.text,'editing',self.editor)
        self.assertEqual(current.started_at,timezone.localdate())
        with self.assertRaises(ValidationError):complete_stage(current,self.editor,None)
        complete_stage(current,self.editor,timezone.localdate())
        current.refresh_from_db();self.assertIsNotNone(current.ended_at)
        with self.assertRaises(ValidationError):self.run_import()

    def test_partial_dates_and_invalid_future_dates(self):
        yesterday=(timezone.localdate()-timedelta(days=1)).isoformat()
        self.run_import(rows=[{**self.rows[0],'ended_at':yesterday}])
        stage=S.objects.get(text=self.text,stage_type='editing')
        self.assertIsNone(stage.started_at);self.assertEqual(stage.ended_at.isoformat(),yesterday)

    def test_invalid_dates_roll_back_assignments(self):
        with self.assertRaises(ValidationError):
            self.run_import(rows=[{**self.rows[0],'ended_at':(timezone.localdate()+timedelta(days=1)).isoformat()}])
        self.assertFalse(A.objects.filter(text=self.text).exists())
        self.assertFalse(S.objects.filter(text=self.text).exists())


    def test_unknown_dates_excluded_from_period_reports_and_stalls(self):
        self.run_import('first_proofreading')
        kwargs = dict(user=self.superuser, stage_roles={'editing':'editor'}, people_role_name='Redaktor', people_context_name='editors')
        all_rows = workflow_activity_context(params=QueryDict(''), **kwargs)['activity_rows']
        self.assertTrue(any(row['title'] == self.text.title for row in all_rows))
        dated_rows = workflow_activity_context(params=QueryDict('date_from=2000-01-01'), **kwargs)['activity_rows']
        self.assertFalse(any(row['title'] == self.text.title for row in dated_rows))
        self.assertFalse(any(self.text.title in issue['detail'] for issue in integrity_issues()))
        context = workflow_inactivity_context(user=self.superuser, params=QueryDict(''))
        self.assertNotIn(self.text.title, str(context))

    def test_ready_anthology_cannot_gain_pending_work(self):
        self.anthology.status='ready';self.anthology.save(update_fields=['status'])
        with self.assertRaises(ValidationError):self.run_import('first_proofreading')
        self.assertTrue(self.run_import())

    def test_import_rejects_continuation_before_completed_stage(self):
        with self.assertRaises(ValidationError):
            self.run_import('ready_for_editing')
        self.assertFalse(S.objects.filter(text=self.text).exists())
