import copy
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.db import IntegrityError, transaction

from authors.models import Author
from people.models import Person
from texts.models import Anthology, Text
from workflow.models import WorkflowStage as Stage, WorkflowRoleAssignment as Assignment


class CompletedTableTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)/'import.json'
        self.report = Path(self.folder.name)/'report'
        self.anthology = Anthology.objects.create(title='Antologia archiwalna', status='ready')
        self.person = {'first_name':'Anna','last_name':'Testowa'}
        self.data = {'schema_version':1,'source':'table-test','texts':[
            {'source_row':1,'title':'Gotowe opowiadanie','anthology':self.anthology.title,
             'length':12345,'length_source':{'sha256':'test-only'},
             'authors':[{'first_name':'Jan','last_name':'Autor'}],
             'stages':[{'stage_type':kind,'person':dict(self.person),'source_column':'Tabela'}
                       for kind in ['editing','editing','first_verification','second_verification','fourth_verification']]}]}

    def run_import(self, apply=True):
        self.path.write_text(json.dumps(self.data),encoding='utf-8')
        call_command('import_completed_table',str(self.path),apply=apply,report=str(self.report),stdout=StringIO())
        return json.loads(self.report.with_suffix('.json').read_text(encoding='utf-8'))

    def test_preview_does_not_create_any_records(self):
        report=self.run_import(False)
        self.assertIn('podgląd',report['mode'])
        for model in (Text,Person,Author,get_user_model(),Stage,Assignment):self.assertEqual(model.objects.count(),0)

    def test_ready_anthology_repeated_work_blank_dates_and_no_login(self):
        self.run_import()
        text=Text.objects.get()
        self.assertEqual(text.anthology,self.anthology)
        self.assertEqual(text.length,12345)
        self.assertEqual(list(Stage.objects.filter(stage_type='editing').order_by('execution_number').values_list('execution_number',flat=True)),[1,2])
        self.assertEqual(Assignment.objects.filter(role='editor').count(),2)
        self.assertEqual(Assignment.objects.filter(role='editor',is_current=True).count(),1)
        self.assertEqual(Stage.objects.filter(imported_completed=True,is_completed=True).count(),5)
        self.assertEqual(Stage.objects.filter(stage_type='ready',is_current=True,is_released=True).count(),1)
        self.assertEqual(Stage.objects.filter(stage_type='fourth_verification',is_current=False,is_released=False).count(),1)
        self.assertFalse(Stage.objects.filter(started_at__isnull=False).exists())
        self.assertFalse(Stage.objects.filter(ended_at__isnull=False).exists())
        self.assertFalse(Stage.objects.filter(queued_at__isnull=False).exists())
        self.assertFalse(Assignment.objects.filter(assigned_at__isnull=False).exists())
        person=Person.objects.select_related('user').get()
        self.assertIsNone(person.email)
        self.assertIsNone(Author.objects.get().email)
        self.assertEqual(person.user.email,'')
        self.assertFalse(person.is_active or person.user.is_active or person.user.is_staff or person.user.is_superuser)
        self.assertFalse(person.user.has_usable_password() or person.roles.exists() or person.user.groups.exists())

    def test_repeat_is_noop_and_changed_input_is_rejected(self):
        self.run_import()
        before=list(Stage.objects.order_by('pk').values())
        report=self.run_import()
        self.assertEqual(report['counts'],{})
        self.assertEqual(report['texts'][0]['result'],'bez zmian')
        self.assertEqual(before,list(Stage.objects.order_by('pk').values()))
        self.data['texts'][0]['length']+=1
        with self.assertRaises(CommandError):self.run_import()
        self.assertEqual(Text.objects.get().length,12345)

    def test_existing_profile_keeps_email_roles_and_account(self):
        user=get_user_model().objects.create_user(username='existing',email='keep@example.com',password='test',**self.person)
        person=Person.objects.create(**self.person,user=user,email='profile@example.com')
        self.run_import()
        person.refresh_from_db();user.refresh_from_db()
        self.assertEqual(person.email,'profile@example.com')
        self.assertEqual(user.email,'keep@example.com')
        self.assertTrue(user.is_active and user.has_usable_password())
        self.assertEqual(Person.objects.count(),1)
        self.assertEqual(get_user_model().objects.count(),1)

    def test_existing_profile_without_account_gets_inactive_account(self):
        person=Person.objects.create(**self.person,email=None,is_active=True)
        self.run_import()
        person.refresh_from_db()
        self.assertTrue(person.is_active)
        self.assertFalse(person.user.is_active)
        self.assertEqual(Person.objects.count(),1)

    def test_two_missing_people_can_both_have_empty_email(self):
        self.data['texts'][0]['stages'][1]['person']['first_name']='Ewa'
        self.run_import()
        self.assertEqual(Person.objects.filter(email__isnull=True).count(),2)
        self.assertEqual(get_user_model().objects.filter(email='').count(),2)

    def test_missing_and_nonempty_anthology_are_rejected(self):
        self.data['texts'][0]['anthology']='Nie istnieje'
        with self.assertRaises(CommandError):self.run_import()
        self.assertEqual(Anthology.objects.count(),1)
        self.data['texts'][0]['anthology']=self.anthology.title
        Text.objects.create(title='Zajęte',length=1,anthology=self.anthology)
        with self.assertRaises(CommandError):self.run_import()
        self.assertEqual(Text.objects.count(),1)
        self.assertFalse(Person.objects.exists())

    def test_ambiguous_name_rolls_back_all_rows_and_reports_conflict(self):
        Person.objects.create(**self.person,email=None)
        Person.objects.create(**self.person,email=None)
        with self.assertRaises(CommandError):self.run_import()
        self.assertFalse(Text.objects.exists() or Author.objects.exists() or get_user_model().objects.exists())
        report=json.loads(self.report.with_suffix('.json').read_text(encoding='utf-8'))
        self.assertIn('Niejednoznaczna',report['conflicts'][0]['error'])

    def test_error_in_later_row_rolls_back_earlier_work_and_profiles(self):
        bad=copy.deepcopy(self.data['texts'][0]);bad.update(source_row=2,title='Drugi tekst')
        bad['stages'][0]['person']['person_id']=999999
        self.data['texts'].append(bad)
        with self.assertRaises(CommandError):self.run_import()
        self.assertFalse(Text.objects.exists() or Person.objects.exists() or Author.objects.exists() or get_user_model().objects.exists())

    def test_missing_measured_length_blocks_import(self):
        self.data['texts'][0]['length']=None
        with self.assertRaises(CommandError):self.run_import()
        self.assertFalse(Text.objects.exists())

    def test_explicit_profile_id_can_resolve_a_name_variant(self):
        person=Person.objects.create(first_name='Anna Maria',last_name='Testowa',email=None)
        for stage in self.data['texts'][0]['stages']:stage['person']['person_id']=person.pk
        self.run_import()
        self.assertEqual(Person.objects.count(),1)
        self.assertEqual(get_user_model().objects.get().first_name,'Anna Maria')

    def test_verifiers_5_to_7_are_completed_history_visible_in_profile(self):
        from workflow.catalog import active_role_choices, active_stage_choices
        from core.selectors.people import imported_work_summary
        for kind in ('fifth_verification','sixth_verification','seventh_verification'):
            self.data['texts'][0]['stages'].append({'stage_type':kind,'person':dict(self.person),'source_column':'Weryfikacja'})
        self.run_import()
        summary={r['role']:r for r in imported_work_summary(Person.objects.get())}
        for number,kind in zip((5,6,7),('fifth_verification','sixth_verification','seventh_verification')):
            role=f'verifier_{number}'
            self.assertEqual(summary[role]['executions'],1)
            self.assertNotIn(role,dict(active_role_choices()))
            self.assertNotIn(kind,dict(active_stage_choices()))
            stage=Stage.objects.get(stage_type=kind)
            self.assertTrue(stage.is_completed and stage.imported_completed)
            self.assertFalse(stage.is_current or stage.is_released or stage.assignment.is_current)
            with self.assertRaises(IntegrityError),transaction.atomic():
                Stage.objects.filter(pk=stage.pk).update(is_current=True)
            with self.assertRaises(IntegrityError),transaction.atomic():
                Assignment.objects.filter(pk=stage.assignment_id).update(is_current=True)
        self.assertFalse(self.report.with_suffix('.html').exists())

    def test_selected_anthology_ignores_previously_modified_other_anthology(self):
        self.run_import()
        old=Text.objects.get();old.length+=1;old.save(update_fields=['length'])
        anthology=Anthology.objects.create(title='Zbrodnia doskonała',status='ready')
        extra=copy.deepcopy(self.data['texts'][0]);extra.update(source_row=2,title='Nowy tekst',anthology=anthology.title)
        self.data['texts'].append(extra)
        self.path.write_text(json.dumps(self.data),encoding='utf-8')
        call_command('import_completed_table',str(self.path),apply=True,anthology=['Zbrodnia doskonała'],report=str(self.report),stdout=StringIO())
        self.assertEqual(Text.objects.count(),2)
        old.refresh_from_db();self.assertEqual(old.length,12346)
        self.assertFalse(self.report.with_suffix('.html').exists())

    def test_unknown_anthology_filter_blocks_write(self):
        self.path.write_text(json.dumps(self.data),encoding='utf-8')
        with self.assertRaises(CommandError):
            call_command('import_completed_table',str(self.path),apply=True,anthology=['Literówka'],report=str(self.report),stdout=StringIO())
        self.assertFalse(Text.objects.exists())
