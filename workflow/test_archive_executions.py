import json
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse

from texts.models import Anthology, Text
from people.models import Person
from authors.models import Author
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.archive_executions import SOURCE, normalized_archive
from workflow.labels import assignment_label, stage_label


class ArchiveExecutionTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        Anthology.objects.create(title='Archiwum',status='ready')
        self.data = {'schema_version':1,'source':SOURCE,'texts':[
            {'source_row':i,'title':f'Tekst {i}','anthology':'Archiwum','length':100,
             'length_source':{'sha256':'fixture'},'authors':[{'first_name':'Anna','last_name':'Autorka'}],
             'stages':[{'stage_type':kind,'source_column':'fixture','person':{'first_name':'Ilona','last_name':'Skrzypczak'}}
                       for kind in ['editing','editing','first_proofreading','second_proofreading',
                                    'first_verification','second_verification','seventh_verification']]}
            for i in range(1,30)]}
        self.before=self.folder/'before.json'; self.after=self.folder/'after.json'
        self.before.write_text(json.dumps(self.data),encoding='utf-8')
        self.after.write_text(json.dumps(normalized_archive(self.data)),encoding='utf-8')

    def initial(self):
        call_command('import_completed_table',str(self.before),apply=True,report=str(self.folder/'old'),stdout=StringIO())

    def repair(self, apply=True):
        call_command('repair_archive_executions',before=str(self.before),after=str(self.after),
                     apply=apply,report=str(self.folder/'repair'),stdout=StringIO())
        return json.loads((self.folder/'repair.json').read_text(encoding='utf-8'))

    def snapshot(self):
        return {m._meta.label:list(m.objects.order_by('pk').values()) for m in [Text,S,A,Person,Author,get_user_model()]}

    def test_preview_apply_rerun_and_normal_import_preserve_ids_and_duplicate_people(self):
        self.initial();before=self.snapshot()
        self.repair(False);self.assertEqual(before,self.snapshot())
        report=self.repair();self.assertEqual(report['counts'],{'korekta':29})
        after=self.snapshot()
        for label,rows in before.items():self.assertEqual([r['id'] for r in rows],[r['id'] for r in after[label]])
        self.assertEqual(Person.objects.get().last_name,'Żurawska')
        self.assertEqual(get_user_model().objects.get().last_name,'Żurawska')
        for text in Text.objects.all():
            for kind,count in [('editing',2),('first_proofreading',2),('first_verification',3)]:
                stages=list(S.objects.filter(text=text,stage_type=kind).order_by('execution_number'))
                self.assertEqual([s.execution_number for s in stages],list(range(1,count+1)))
                self.assertTrue(all(s.is_completed and s.imported_completed for s in stages))
                expected = {'editing':'Redaktor', 'first_proofreading':'Korektor 1', 'first_verification':'Weryfikator 1'}[kind]
                for number, stage in enumerate(stages, 1):
                    label = expected if number == 1 else f'{expected} (wyk. {number})'
                    self.assertEqual(stage_label(stage), label)
                    self.assertEqual(assignment_label(stage.assignment), label)
                self.assertNotIn('przebieg',str(stages[0].assignment))
        self.assertEqual(S.objects.filter(stage_type='ready').count(),29)
        report=self.repair();self.assertEqual(report['counts'],{'bez zmian':29})
        self.assertEqual(after,self.snapshot())
        call_command('import_completed_table',str(self.after),apply=True,report=str(self.folder/'new'),stdout=StringIO())
        self.assertEqual(after,self.snapshot())
        admin=get_user_model().objects.create_superuser(username='render-admin',email='admin@example.com',password='test')
        self.client.force_login(admin)
        text=Text.objects.get(import_source_row=1)
        response=self.client.get(reverse('core:assigned_text_detail',args=[text.pk]))
        self.assertEqual(response.status_code,200)
        team=response.context['team_members']
        self.assertEqual(len(team),7)
        self.assertEqual([m['label'] for m in team if m['role']=='verifier_1'],
                         ['Weryfikator 1','Weryfikator 1 (wyk. 2)','Weryfikator 1 (wyk. 3)'])
        self.assertContains(response,'Korektor 1 (wyk. 2)')
        self.assertContains(response,'Redaktor')

    def test_late_conflict_rolls_back_previous_changes_and_name(self):
        self.initial()
        last=Text.objects.get(import_source_row=29)
        S.objects.filter(text=last,stage_type='editing',execution_number=1).update(ended_at='2020-01-01')
        before=self.snapshot()
        with self.assertRaises(CommandError):self.repair()
        self.assertEqual(before,self.snapshot())

    def test_existing_zurawska_reused_without_deleting_old_profile(self):
        self.initial();old=Person.objects.get()
        u=get_user_model().objects.create(username='existing',first_name='Ilona',last_name='Żurawska',email='keep@example.com')
        new=Person.objects.create(first_name='Ilona',last_name='Żurawska',email='keep@example.com',user=u)
        self.repair()
        old.refresh_from_db();new.refresh_from_db();u.refresh_from_db()
        self.assertEqual(old.last_name,'Skrzypczak')
        self.assertEqual(new.email,'keep@example.com')
        self.assertEqual(u.email,'keep@example.com')
        self.assertEqual(set(A.objects.values_list('assigned_to_id',flat=True)),{u.pk})

    def test_no_existing_import_can_be_followed_by_fresh_import(self):
        result=self.repair();self.assertEqual(len(result['missing']),29)
        call_command('import_completed_table',str(self.after),apply=True,report=str(self.folder/'new'),stdout=StringIO())
        self.assertEqual(Text.objects.count(),29)
        self.assertFalse(S.objects.filter(stage_type='seventh_verification').exists())
        self.assertEqual(self.repair()['counts'],{'bez zmian':29})

    def test_ambiguous_ilona_rolls_back(self):
        self.initial()
        Person.objects.create(first_name='Ilona',last_name='Skrzypczak',email=None)
        before=self.snapshot()
        with self.assertRaises(CommandError):self.repair()
        self.assertEqual(before,self.snapshot())

    def test_other_sources_keep_operational_labels(self):
        text=Text(title='Inny',length=1,import_source='other',import_source_row=1)
        s=S(text=text,stage_type='first_verification',execution_number=1,imported_completed=True)
        self.assertEqual(stage_label(s),'Pierwsza weryfikacja')
