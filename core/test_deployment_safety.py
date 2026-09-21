"""First-use regressions: accounts, anthology creation and author-note conflicts."""
from io import StringIO
from unittest.mock import patch
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import PasswordResetForm
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from authors.models import Author, AuthorNote
from core.admin import AccountCreationForm
from core.edit_versions import version_of
from people.models import Person, Role
from texts.models import Anthology, AnthologyTask, Text, Review

User = get_user_model()
PASSWORD = 'Useful-test-password!37'


class DeploymentSafetyTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@example.com', PASSWORD)
        self.client.force_login(self.admin)

    def account_form(self, email, username='separate-nickname'):
        return AccountCreationForm(data={
            'username': username, 'first_name': 'Anna', 'last_name': 'Testowa',
            'email': email, 'password1': PASSWORD, 'password2': PASSWORD,
        })

    def test_email_login_and_password_reset_with_different_username(self):
        form = self.account_form('Anna@Example.com')
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.email, 'anna@example.com')
        self.assertEqual(authenticate(username=' ANNA@example.com ', password=PASSWORD), user)
        self.assertIsNone(authenticate(username=user.username, password=PASSWORD))
        self.assertEqual(list(PasswordResetForm().get_users(user.email)), [user])
        self.client.logout()
        response = self.client.post(reverse('login'), {'username':'ANNA@example.com', 'password':PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['_auth_user_id'], str(user.pk))

    def test_account_email_is_required_unique_and_inactive_cannot_login(self):
        self.assertFalse(self.account_form('').is_valid())
        self.assertFalse(self.account_form('ADMIN@example.com').is_valid())
        self.admin.is_active = False
        self.admin.save()
        self.assertIsNone(authenticate(username=self.admin.email, password=PASSWORD))

    def test_ambiguous_email_does_not_authenticate(self):
        User.objects.create_user('duplicate', self.admin.email, PASSWORD)
        self.assertIsNone(authenticate(username=self.admin.email, password=PASSWORD))

    def test_superuser_command_email_login_and_duplicate_rejection(self):
        with patch.dict('os.environ', {'DJANGO_SUPERUSER_PASSWORD':PASSWORD}):
            call_command('createsuperuser', username='console', email='Console@Example.com',
                         first_name='Anna', last_name='Testowa', interactive=False, stdout=StringIO())
        self.assertTrue(authenticate(username='console@example.com', password=PASSWORD).is_superuser)
        for email in ('CONSOLE@example.com', ''):
            with self.assertRaises(CommandError):
                call_command('createsuperuser', username='another', email=email, first_name='Jan',
                             last_name='Testowy', interactive=False, stdout=StringIO())
        self.assertFalse(User.objects.filter(username='another').exists())

    def anthology_data(self, tasks):
        data = {'title':'Nowa antologia','status':'in_preparation','cover_status':'not_started',
                'print_status':'no','production_tasks-TOTAL_FORMS':str(len(tasks)),
                'production_tasks-INITIAL_FORMS':'0','production_tasks-MIN_NUM_FORMS':'0',
                'production_tasks-MAX_NUM_FORMS':'3','_save':'Save'}
        for i, task in enumerate(tasks):
            data.update({f'production_tasks-{i}-{key}':value for key,value in task.items()})
        return data

    def test_new_anthology_with_commissioned_inline_task(self):
        person = Person.objects.create(first_name='Jan',last_name='Testowy',email='person@example.com')
        response = self.client.post(reverse('admin:texts_anthology_add'), self.anthology_data([
            {'task_type':'typesetting','status':'commissioned','assigned_to':person.pk},
            {'task_type':'blurb','status':'not_commissioned'},
        ]))
        self.assertEqual(response.status_code, 302)
        anthology = Anthology.objects.get()
        self.assertEqual(anthology.production_tasks.count(), 3)
        task = anthology.production_tasks.get(task_type='typesetting')
        self.assertEqual(task.assigned_to, person)
        self.assertEqual(task.status, 'commissioned')
        self.assertIsNotNone(task.commissioned_at)

    def test_new_anthology_without_inline_tasks(self):
        response = self.client.post(reverse('admin:texts_anthology_add'), self.anthology_data([]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AnthologyTask.objects.count(), 3)

    def test_invalid_inline_rolls_back_whole_anthology(self):
        response = self.client.post(reverse('admin:texts_anthology_add'), self.anthology_data([
            {'task_type':'typesetting','status':'commissioned'},
        ]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Anthology.objects.exists())
        self.assertFalse(AnthologyTask.objects.exists())

    def test_stale_author_form_cannot_overwrite_newer_note(self):
        author = Author.objects.create(first_name='Anna',last_name='Testowa',email='writer@example.com')
        note = AuthorNote.objects.create(author=author,created_by=self.admin,content='Old note')
        url = reverse('admin:authors_author_change',args=[author.pk])
        response = self.client.get(url)
        token = response.wsgi_request.edit_version_token
        note_url = reverse('admin:authors_authornote_change',args=[note.pk])
        note_token = self.client.get(note_url).wsgi_request.edit_version_token
        response = self.client.post(note_url, {'_edit_version':note_token,'author':author.pk,
                                               'content':'New note','_save':'Save'})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(url, {'_edit_version':token,'first_name':'Anna','last_name':'Testowa',
            'email':author.email,'contact':'on','notes-TOTAL_FORMS':'1','notes-INITIAL_FORMS':'1',
            'notes-MIN_NUM_FORMS':'0','notes-MAX_NUM_FORMS':'1000','notes-0-id':note.pk,
            'notes-0-author':author.pk,'notes-0-content':'Old note','_save':'Save'})
        self.assertEqual(response.status_code, 409)
        note.refresh_from_db()
        self.assertEqual(note.content, 'New note')

    def test_note_creation_deletion_and_move_invalidate_parent_versions(self):
        a = Author.objects.create(first_name='A',last_name='Test',email='a@example.com')
        b = Author.objects.create(first_name='B',last_name='Test',email='b@example.com')
        before = version_of(a)
        note = AuthorNote.objects.create(author=a,content='Note')
        self.assertGreater(version_of(a), before)
        av, bv = version_of(a), version_of(b)
        note.author = b
        note.save()
        self.assertGreater(version_of(a), av)
        self.assertGreater(version_of(b), bv)
        before = version_of(b)
        note.delete()
        self.assertGreater(version_of(b), before)

    def test_role_bootstrap_is_idempotent_and_creates_no_business_data(self):
        Role.objects.all().delete()
        existing = Role.objects.create(name='recenzent')
        before = (User.objects.count(),Person.objects.count(),Author.objects.count(),Text.objects.count(),Review.objects.count())
        call_command('ensure_team_roles',stdout=StringIO())
        count = Role.objects.count()
        call_command('ensure_team_roles',stdout=StringIO())
        self.assertEqual(Role.objects.count(),count)
        self.assertEqual(Role.objects.get(name__iexact='Recenzent').pk,existing.pk)
        for name in ('Redaktor','Korektor','Weryfikator','Ilustrator','Korektor audiobooków','Koordynator'):
            self.assertTrue(Role.objects.filter(name=name).exists())
        self.assertFalse(Role.objects.filter(name__in=('Koordynator zespołu','Weryfikator 4')).exists())
        self.assertEqual(before,(User.objects.count(),Person.objects.count(),Author.objects.count(),Text.objects.count(),Review.objects.count()))
