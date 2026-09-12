import copy
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command, CommandError
from django.forms import modelform_factory
from django.test import TestCase
from django.contrib.auth import get_user_model

from authors.models import Author
from people.models import Person
from texts.models import Text, HistoricalTextAssignment
from workflow.models import WorkflowStage, WorkflowRoleAssignment


class FullHistoryImportTests(TestCase):
    def setUp(self):
        self.data = json.loads((Path(__file__).resolve().parents[1]/'import_data/teksty_historyczne.json').read_text())

    def run_import(self, data, commit=False):
        with TemporaryDirectory() as tmp:
            p = Path(tmp)/'data.json'; p.write_text(json.dumps(data))
            call_command('import_historical_texts', str(p), commit=commit, stdout=StringIO())

    def test_full_dataset_preview_commit_repeat_and_no_live_work(self):
        self.run_import(self.data)
        self.assertEqual(Text.objects.count(), 0)
        self.assertEqual(Person.objects.count(), 0)
        self.assertEqual(Author.objects.count(), 0)
        self.run_import(self.data, True)
        self.assertEqual(Text.objects.count(), 135)
        self.assertEqual(Author.objects.count(), 103)
        self.assertEqual(Person.objects.count(), 149)
        self.assertEqual(HistoricalTextAssignment.objects.count(), 1239)
        self.assertEqual(WorkflowStage.objects.filter(stage_type='ready').count(), 127)
        self.assertEqual(WorkflowStage.objects.filter(stage_type='withdrawn').count(), 8)
        self.assertFalse(WorkflowRoleAssignment.objects.exists())
        self.assertFalse(WorkflowStage.objects.exclude(started_at=None, ended_at=None).exists())
        self.assertFalse(Person.objects.filter(is_active=True).exists())
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(Author.objects.exclude(email=None).exists())
        self.assertTrue(Author.objects.get(first_name='Tomasz', last_name='Tumski').is_blacklisted)
        self.run_import(self.data, True)
        self.assertEqual(HistoricalTextAssignment.objects.count(), 1239)
        self.assertEqual(Person.objects.count(), 149)

    def test_existing_people_and_real_email_are_preserved(self):
        subset = copy.deepcopy(self.data); subset['texts'] = subset['texts'][:1]
        author_data = subset['texts'][0]['authors'][0]
        person_data = subset['texts'][0]['historical_assignments'][0]['person']
        author = Author.objects.create(**{**author_data, 'email':'real-author@example.com'}, contact=True)
        person = Person.objects.create(**{**person_data, 'email':'real-person@example.com'}, is_active=True)
        self.run_import(subset, True)
        author.refresh_from_db(); person.refresh_from_db()
        self.assertEqual(author.email, 'real-author@example.com')
        self.assertTrue(author.contact)
        self.assertEqual(person.email, 'real-person@example.com')
        self.assertTrue(person.is_active)
        self.assertEqual(HistoricalTextAssignment.objects.filter(person=person).count(), 1)

    def test_ambiguous_name_and_bad_late_row_roll_back_everything(self):
        first = self.data['texts'][0]['authors'][0]
        Author.objects.create(**{**first,'email':'a@example.com'})
        Author.objects.create(**{**first,'email':'b@example.com'})
        with self.assertRaises(CommandError):self.run_import(self.data, True)
        self.assertFalse(Text.objects.exists())
        Author.objects.all().delete()
        broken = copy.deepcopy(self.data)
        broken['texts'][1]['historical_assignments'][-1]['ended_at'] = '2020-01-01'
        with self.assertRaises(CommandError):self.run_import(broken, True)
        self.assertFalse(Text.objects.exists())
        self.assertFalse(Person.objects.exists())
        self.assertFalse(Author.objects.exists())

    def test_existing_modified_text_is_not_overwritten(self):
        subset = copy.deepcopy(self.data); subset['texts'] = subset['texts'][:1]
        self.run_import(subset, True)
        text = Text.objects.get(); text.coordinator_note='Nowa uwaga'; text.save()
        with self.assertRaises(CommandError):self.run_import(subset, True)
        text.refresh_from_db(); self.assertEqual(text.coordinator_note, 'Nowa uwaga')

    def test_regular_forms_still_require_email(self):
        for model in (Author,Person):
            self.assertTrue(modelform_factory(model, fields=['email'])().fields['email'].required)

    def test_imported_null_emails_and_inactive_profile_render(self):
        from django.urls import reverse
        subset = copy.deepcopy(self.data); subset['texts'] = subset['texts'][:1]
        self.run_import(subset, True)
        user = get_user_model().objects.create_superuser(username='history_admin', email='admin@example.com', password='test')
        self.client.force_login(user)
        text = Text.objects.get()
        person = Person.objects.first()
        for route, args in [('core:assigned_text_detail', [text.pk]), ('core:person_detail', [person.pk]), ('admin:texts_text_changelist', [])]:
            response = self.client.get(reverse(route, args=args))
            self.assertEqual(response.status_code, 200)
        self.assertEqual(text.author_emails, '')
