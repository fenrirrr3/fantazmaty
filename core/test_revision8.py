from core.testing_forms import post_form
from datetime import timedelta
from io import StringIO
import json
import tempfile
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from authors.models import Author
from core.tests import CoreTestDataMixin
from core.author_contact import stored_author_phone
from core.correction_forms import CorrectionForm
from core.file_forms import TextFileForm
from core.supervision import anthology_checklist, anthology_credits, integrity_issues, duplicate_candidates
from people.models import Person
from texts.models import Anthology, Text, Review, ReviewAssignment, HistoricalTextAssignment
from workflow.models import WorkflowStage, WorkflowRoleAssignment


class Revision8Tests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.text = Text.objects.create(title='Kraina niezwykłych smoków', anthology=self.anthology, length=100)
        self.text.authors.add(self.author)
        self.client.force_login(self.superuser)

    def assign(self, **kwargs):
        values = dict(text=self.text, role='editor', assigned_to=self.editor, workflow_cycle=1)
        values.update(kwargs)
        return WorkflowRoleAssignment.objects.create(**values)

    def test_phone_has_single_source(self):
        Review.objects.create(title='Dawny', anthology=self.anthology, author=self.author, phone_number='999', old_reviews=True)
        self.assertEqual(stored_author_phone(self.author), '')
        self.author.phone_number='123 456 789'; self.author.save()
        response=self.client.get(reverse('core:author_suggestions'),{'q':self.author.email})
        self.assertEqual(response.json()['results'][0]['phone_number'],'123 456 789')
        self.assertContains(self.client.get(reverse('core:author_detail',args=[self.author.pk])),'123 456 789')

    def test_correction_choices_only_ready_published(self):
        ready=Anthology.objects.create(title='Gotowa',status='ready')
        published=Anthology.objects.create(title='Wydana',status='published')
        self.assertSetEqual(set(CorrectionForm().fields['anthology'].queryset.values_list('pk',flat=True)),{ready.pk,published.pk})
        form=CorrectionForm(data={'anthology':self.anthology.pk,'fragment':'a','problem':'b','suggestion':'c'})
        self.assertFalse(form.is_valid()); self.assertIn('anthology',form.errors)

    def test_search_hides_email_even_if_team_profile_matches(self):
        person=Person.objects.create(first_name='Autor',last_name='Kontrolny',email=self.author.email,is_active=True)
        self.client.force_login(self.editor)
        response=self.client.get(reverse('core:global_search'),{'q':'Kontrolny'})
        self.assertNotContains(response,self.author.email)
        self.assertEqual(response.context['people'][0]['email'],'')
        self.assertEqual(self.client.get(reverse('core:global_search'),{'q':self.author.email}).context['people'],[])
        self.assign()
        response=self.client.get(reverse('core:global_search'),{'q':'Kontrolny'})
        self.assertContains(response,self.author.email)

    def test_search_author_scope_updates_after_assignment(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse('core:global_search'),{'q':self.author.email}).context['authors'],[])
        self.assign()
        self.assertEqual(len(self.client.get(reverse('core:global_search'),{'q':self.author.email}).context['authors']),1)

    def test_file_url_permission_and_validation(self):
        url=reverse('core:update_text_file',args=[self.text.pk])
        self.client.force_login(self.editor)
        self.assertEqual(post_form(self.client, url,{'file_url':'https://dropbox.com/example'}).status_code,403)
        self.assign()
        self.assertEqual(post_form(self.client, url,{'file_url':'javascript:alert(1)'}).status_code,400)
        self.assertEqual(post_form(self.client, url,{'file_url':'https://dropbox.com/example'}).status_code,302)
        self.text.refresh_from_db();self.assertEqual(self.text.file_url,'https://dropbox.com/example')
        self.assertContains(self.client.get(reverse('core:assigned_text_detail',args=[self.text.pk])),'https://dropbox.com/example')

    def test_supervision_is_superuser_only(self):
        urls=[reverse('core:data_integrity'),reverse('core:person_permissions',args=[self.editor.person_profile.pk])]
        for url in urls:self.assertEqual(self.client.get(url).status_code,200)
        self.client.force_login(self.editor)
        for url in urls:self.assertEqual(self.client.get(url).status_code,403)

    def test_readonly_integrity_finds_orphan_and_inactive(self):
        WorkflowStage.objects.create(text=self.text,stage_type='editing',started_at=timezone.localdate(),workflow_cycle=1)
        self.assertTrue(any(i['label']=='Rozpoczęty etap bez wykonawcy' for i in integrity_issues()))
        self.assign();self.editor.is_active=False;self.editor.save()
        self.assertTrue(any(i['label']=='Przydział do nieaktywnej osoby' for i in integrity_issues()))
        self.assertEqual(WorkflowStage.objects.filter(text=self.text).count(),1)

    def test_unknown_historical_dates_are_not_started(self):
        self.text.is_historical=True;self.text.save()
        HistoricalTextAssignment.objects.create(text=self.text, role='editor', person_name='Historyczna osoba', is_completed=True)
        self.assertFalse(any(i['label']=='Rozpoczęty etap bez wykonawcy' for i in integrity_issues()))

    def test_checklist_requires_ready_and_contract(self):
        self.author.has_contract=False;self.author.save()
        labels=[i['label'] for i in anthology_checklist(self.anthology)]
        self.assertIn('Tekst nie jest gotowy',labels);self.assertIn('Brak potwierdzonej umowy',labels)
        WorkflowStage.objects.create(text=self.text,stage_type='ready',workflow_cycle=1)
        self.author.has_contract=True;self.author.save()
        labels=[i['label'] for i in anthology_checklist(self.anthology)]
        self.assertNotIn('Tekst nie jest gotowy',labels);self.assertNotIn('Brak potwierdzonej umowy',labels)

    def test_credits_include_completed_work_without_dates(self):
        self.text.is_historical=True;self.text.save()
        HistoricalTextAssignment.objects.create(text=self.text,role='editor',person=self.editor.person_profile,person_name='Redaktor',is_completed=True)
        credits=anthology_credits(self.anthology)
        self.assertEqual(len(credits),1);self.assertIn(self.text.title,credits[0]['works'])
        response=self.client.get(reverse('core:anthology_credits_csv',args=[self.anthology.pk]))
        self.assertContains(response,self.text.title)

    def test_anthology_detail_renders_and_hides_checklist_from_member(self):
        url=reverse('core:anthology_detail',args=[self.anthology.pk])
        self.assertContains(self.client.get(url),'Lista kontrolna przed wydaniem')
        self.client.force_login(self.editor)
        self.assertNotContains(self.client.get(url),'Lista kontrolna przed wydaniem')

    def test_fuzzy_duplicates_scoped_to_anthology_and_author(self):
        rows=duplicate_candidates('Kraina niezwyklych smokow!',self.anthology.pk,[self.author.pk])
        self.assertEqual(len(rows),1)
        other=Anthology.objects.create(title='Inny nabór')
        self.assertEqual(duplicate_candidates(self.text.title,other.pk,[self.author.pk]),[])
        self.assertEqual(duplicate_candidates(self.text.title,self.anthology.pk,[]),[])
        self.assertEqual(self.client.get(reverse('core:data_integrity'),{'tab':'duplicates'}).status_code,200)

    def team_file(self, dropbox='', **ids):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        file=Path(temp.name)/'team.tsv'
        file.write_text('Nazwisko i imię\tE-mail\tE-mail Dropbox\tFunkcja\tID osoby\tID konta\tID autora\n'+f'Nowak Anna\tnew@example.com\t{dropbox}\tRedaktor\t{ids.get("person","")}\t{ids.get("user","")}\t{ids.get("author","")}\n',encoding='utf-8')
        return file

    def test_stable_identity_email_change_blank_preserves_and_clear_removes(self):
        user=get_user_model().objects.create_user('old@example.com',email='old@example.com')
        person=Person.objects.create(first_name='Anna',last_name='Nowak',email=user.email,dropbox_email='box@example.com',user=user)
        call_command('import_team_members',str(self.team_file(person=person.pk,author=self.author.pk)),commit=True,stdout=StringIO())
        person.refresh_from_db();user.refresh_from_db()
        self.assertEqual(person.user_id,user.pk);self.assertEqual(user.username,'old@example.com')
        self.assertEqual(user.email,'new@example.com');self.assertEqual(person.author_profile_id,self.author.pk)
        self.assertEqual(person.dropbox_email,'box@example.com')
        call_command('import_team_members',str(self.team_file('__CLEAR__',person=person.pk)),commit=True,stdout=StringIO())
        person.refresh_from_db();self.assertEqual(person.dropbox_email,'')

    def test_dry_run_report_preserves_database(self):
        file=self.team_file();report=file.with_suffix('.json')
        count=Person.objects.count()
        call_command('import_team_members',str(file),report=str(report),stdout=StringIO())
        self.assertEqual(Person.objects.count(),count)
        data=json.loads(report.read_text());self.assertEqual(data['mode'],'podglad');self.assertEqual(data['models']['osoby']['added'],1)
        self.assertTrue(data['added_roles'])

    def test_conflicting_email_rolls_back(self):
        person=self.editor.person_profile
        self.author.email='new@example.com';self.author.save()
        get_user_model().objects.create_user('collision',email='new@example.com')
        before=person.email
        with self.assertRaises(CommandError):call_command('import_team_members',str(self.team_file(person=person.pk)),commit=True,stdout=StringIO())
        person.refresh_from_db();self.assertEqual(person.email,before)

    def test_history_author_phone_and_email_update_by_id_preserves_relations(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        file=Path(temp.name)/'history.json'
        author_data={'id':self.author.pk,'first_name':self.author.first_name,'last_name':self.author.last_name,'email':'changed@example.com','phone_number':'12345','pseudonym':'Smok'}
        row={'historical_source':'test-identity','historical_source_row':1,'title':'Dawny tekst','anthology':{'title':self.anthology.title},'authors':[author_data],'length':100,'is_historical':True,'coordinator_note':'','historical_assignments':[], 'workflow_stages':[{'stage_type':'ready','started_at':None,'ended_at':None,'is_completed':False,'workflow_cycle':1,'iteration':1}]}
        file.write_text(json.dumps({'schema_version':1,'texts':[row]}))
        call_command('import_historical_texts',str(file),commit=True,stdout=StringIO())
        self.author.refresh_from_db();self.assertEqual(self.author.email,'changed@example.com');self.assertEqual(self.author.phone_number,'12345')
        self.assertTrue(self.text.authors.filter(pk=self.author.pk).exists())
        author_data['phone_number']='';file.write_text(json.dumps({'schema_version':1,'texts':[row]}))
        call_command('import_historical_texts',str(file),commit=True,stdout=StringIO())
        self.author.refresh_from_db();self.assertEqual(self.author.phone_number,'12345')
        author_data['phone_number']='__CLEAR__';file.write_text(json.dumps({'schema_version':1,'texts':[row]}))
        call_command('import_historical_texts',str(file),commit=True,stdout=StringIO())
        self.author.refresh_from_db();self.assertEqual(self.author.phone_number,'')
        stage=WorkflowStage.objects.get(text__historical_source='test-identity')
        self.assertIsNone(stage.started_at);self.assertIsNone(stage.ended_at)

    def test_fuzzy_warning_is_present_in_single_review_form(self):
        from core.intake_forms import SingleReviewForm
        form=SingleReviewForm(data={'author':self.author.pk,'title':'Kraina niezwykłych smokow!','genre':'Fantasy','length':100,'anthology':self.anthology.pk})
        self.assertFalse(form.is_valid())
        self.assertTrue(any('Podobny tytuł' in warning for warning in form.submission_warnings))

    def test_archive_reviewer_is_credited(self):
        review=Review.objects.create(title='Z archiwum',anthology=self.anthology,old_reviews=True,status='accepted')
        ReviewAssignment.objects.create(review=review,historical_person=self.editor.person_profile,position=1,opinion='yes')
        credits=anthology_credits(self.anthology)
        self.assertTrue(any(row['role']=='Recenzent' and 'Z archiwum' in row['works'] for row in credits))

    def test_import_repeated_blank_dropbox_is_not_a_conflict(self):
        file=self.team_file('box@example.com')
        with file.open('a') as stream:stream.write('Nowak Anna\tnew@example.com\t\tWeryfikator\t\t\t\n')
        call_command('import_team_members',str(file),commit=True,stdout=StringIO())
        person=Person.objects.get(email='new@example.com')
        self.assertEqual(person.dropbox_email,'box@example.com')
        self.assertSetEqual(set(person.roles.values_list('name',flat=True)),{'Redaktor','Weryfikator'})

    def test_export_contains_stable_ids(self):
        result=StringIO();call_command('export_team_members',stdout=result)
        self.assertIn('ID osoby\tID konta\tID autora',result.getvalue())
        self.assertNotIn('password',result.getvalue())
