import io
import json
import tempfile
from pathlib import Path
from django.test import TestCase
from django.core.management import call_command, CommandError
from django.contrib.auth import get_user_model
from authors.models import Author
from people.models import Person
from texts.models import Review, ReviewAssignment

class HistoricalReviewImportTests(TestCase):
    def row(self, title='Test archiwalny'):
        return dict(row=1,authors=['Anna Testowa'],title=title,genre='',length=None,
            content_warnings='',anthology='Archiwum',created_at='2020-01-02',status='accepted',
            author_notified_at='2020-01-03',general_notes='Treść notatki',
            assignments=[dict(name='Jan Recenzent',position=1,opinion='yes')])

    def run_import(self, rows, commit=False):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data.json'
            path.write_text(json.dumps(dict(schema_version=1,all_historical=True,reviews=rows)))
            call_command('import_historical_reviews',str(path),commit=commit,stdout=io.StringIO())

    def test_dry_run_and_repeat(self):
        self.run_import([self.row()])
        self.assertEqual(Review.objects.count(),0)
        self.assertEqual(Author.objects.count(),0)
        self.run_import([self.row()],commit=True)
        self.run_import([self.row()],commit=True)
        self.assertEqual(Review.objects.count(),1)
        self.assertEqual(ReviewAssignment.objects.count(),1)
        self.assertEqual(str(Review.objects.get().created_at),'2020-01-02')

    def test_reuses_existing_account_without_changing_contact(self):
        user=get_user_model().objects.create_user(username='recenzent',first_name='Jan',last_name='Recenzent')
        person=Person.objects.create(first_name='Jan',last_name='Recenzent',user=user,email='reviewer@example.com')
        author=Author.objects.create(first_name='Anna',last_name='Testowa',email='author@example.com',has_contract=True)
        self.run_import([self.row()],commit=True)
        assignment=ReviewAssignment.objects.get()
        self.assertEqual(assignment.user_id,user.pk)
        self.assertEqual(assignment.historical_person_id,person.pk)
        author.refresh_from_db()
        self.assertEqual(author.email,'author@example.com')
        self.assertTrue(author.has_contract)
        self.assertEqual(get_user_model().objects.count(),1)

    def test_error_rolls_back_prior_rows(self):
        broken=self.row('Błędny');broken['length']=-1
        with self.assertRaises(CommandError):
            self.run_import([self.row(),broken],commit=True)
        self.assertEqual(Review.objects.count(),0)
        self.assertEqual(Author.objects.count(),0)
        self.assertEqual(Person.objects.count(),0)

    def test_conflict_does_not_overwrite(self):
        self.run_import([self.row()],commit=True)
        changed=self.row();changed['status']='rejected'
        with self.assertRaises(CommandError):
            self.run_import([changed],commit=True)
        self.assertEqual(Review.objects.get().status,'accepted')
