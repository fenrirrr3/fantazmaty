import re
from html import unescape
from unittest.mock import patch
from django.db import transaction
from django.core.management import get_commands
from django.test import TestCase
from django.urls import reverse
from core.tests import CoreTestDataMixin
from core.middleware import fingerprint
from core.testing_forms import post_form
from authors.models import Author
from texts.models import TextNote, Text, Anthology
from core.supervision import all_duplicates


class Simplification23Tests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.superuser)
        self.text = Text.objects.create(title='Próba', anthology=self.anthology, length=1000)

    def token(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="_edit_version" value="([^"]+)"', response.content.decode())
        self.assertIsNotNone(match)
        return unescape(match[1])

    def test_version_cost_does_not_depend_on_notes_and_blocks_stale_write(self):
        detail = reverse('core:assigned_text_detail', args=[self.text.pk])
        token = self.token(detail)
        first = fingerprint(self.text)
        for index in range(30):
            TextNote.objects.create(text=self.text, author=self.superuser, content=f'Notatka {index}')
        with self.assertNumQueries(1):
            self.assertGreater(fingerprint(self.text), first)
        response = self.client.post(reverse('core:update_coordinator_note', args=[self.text.pk]), {'coordinator_note':'Stara karta', '_edit_version':token})
        self.assertEqual(response.status_code, 409)
        self.text.refresh_from_db()
        self.assertNotEqual(self.text.coordinator_note, 'Stara karta')
        response = post_form(self.client, reverse('core:update_coordinator_note', args=[self.text.pk]), {'coordinator_note':'Nowa karta'})
        self.assertEqual(response.status_code, 302)

    def test_version_rollback_and_m2m_reverse_clear(self):
        first = fingerprint(self.text)
        with transaction.atomic():
            self.text.title = 'Wycofana zmiana'
            self.text.save(update_fields=['title'])
            self.assertGreater(fingerprint(self.text), first)
            transaction.set_rollback(True)
        self.assertEqual(fingerprint(self.text), first)
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor23@example.com')
        self.text.authors.add(author)
        changed = fingerprint(self.text)
        self.assertGreater(changed, first)
        author.texts.clear()
        self.assertGreater(fingerprint(self.text), changed)

    def test_admin_explicit_token_and_stale_save(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor23@example.com')
        url = reverse('admin:authors_author_change', args=[author.pk])
        token = self.token(url)
        author.phone_number = '123456789'
        author.save(update_fields=['phone_number'])
        response = self.client.post(url, {'first_name':'Jan', 'last_name':'Autor', 'email':author.email, '_edit_version':token})
        self.assertEqual(response.status_code, 409)

    def test_duplicate_scan_only_runs_on_explicit_valid_selection(self):
        url = reverse('core:data_integrity')
        with patch('core.views.supervision.all_duplicates', return_value=[]) as scan:
            self.assertEqual(self.client.get(url, {'tab':'duplicates'}).status_code, 200)
            self.client.get(url, {'tab':'duplicates', 'anthology':self.anthology.pk})
            self.client.get(url, {'tab':'duplicates', 'run':'1', 'anthology':'invalid'})
            scan.assert_not_called()
            self.client.get(url, {'tab':'duplicates', 'run':'1', 'anthology':self.anthology.pk})
            scan.assert_called_once_with(self.anthology.pk)

    def test_duplicate_scan_does_not_mix_anthologies(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor23@example.com')
        self.text.authors.add(author)
        twin = Text.objects.create(title=self.text.title, anthology=self.anthology, length=1000)
        twin.authors.add(author)
        other = Anthology.objects.create(title='Inna antologia')
        elsewhere = Text.objects.create(title=self.text.title, anthology=other, length=1000)
        elsewhere.authors.add(author)
        self.assertEqual(len(all_duplicates(self.anthology.pk)), 1)
        self.assertEqual(all_duplicates(other.pk), [])

    def test_only_unified_migration_command_remains(self):
        commands = get_commands()
        self.assertIn('import_team_archive', commands)
        for old in ('import_historical_texts', 'import_historical_reviews', 'import_historical_assignments', 'import_extracts', 'import_team_members', 'import_completed_workflow'):
            self.assertNotIn(old, commands)

    def test_deleting_author_invalidates_text_and_proxy_save_shares_version(self):
        from authors.models import BlacklistedAuthor
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor23@example.com')
        self.text.authors.add(author)
        first = fingerprint(author)
        proxy = BlacklistedAuthor.objects.get(pk=author.pk)
        proxy.phone_number = '123456789'
        proxy.save(update_fields=['phone_number'])
        self.assertGreater(fingerprint(author), first)
        before_delete = fingerprint(self.text)
        author.delete()
        self.assertGreater(fingerprint(self.text), before_delete)

    def test_main_settings_keep_template_options_out_of_database_and_passwords(self):
        import os
        import runpy
        from pathlib import Path
        from django.contrib.auth.password_validation import get_password_validators
        environment = {'DJANGO_ENV':'test', 'DJANGO_SECRET_KEY':'settings-test',
                       'DJANGO_DB_NAME':'test', 'DJANGO_DB_USER':'test', 'DJANGO_DB_PASSWORD':'test'}
        with patch.dict(os.environ, environment, clear=True), patch('dotenv.load_dotenv'):
            config = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'fantazmaty' / 'settings.py'))
        self.assertEqual(config['TEMPLATES'][0]['OPTIONS']['builtins'], ['core.templatetags.editing'])
        self.assertNotIn('builtins', config['DATABASES']['default']['OPTIONS'])
        get_password_validators(config['AUTH_PASSWORD_VALIDATORS'])
