from tempfile import TemporaryDirectory

from django.contrib import admin
from django.contrib.admin.models import LogEntry, CHANGE
from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.urls import reverse

from core.edit_versions import version_of
from people.models import Person, Role
from texts.models import Anthology, AnthologyTask, Text


class AdminToolsTests(TestCase):
    def setUp(self):
        spool = TemporaryDirectory()
        self.addCleanup(spool.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=spool.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_superuser('tools', 'tools@example.test', 'test')
        self.client.force_login(self.user)
        self.book = Anthology.objects.create(title='Narzędzia')
        self.story = Text.objects.create(title='Nagranie', anthology=self.book, length=100)
        self.other = Text.objects.create(title='Bez zmian', anthology=self.book, length=100)
        self.url = reverse('admin:texts_text_changelist')

    def test_bulk_blacklist_preserves_unselected_and_records_history_and_revision(self):
        initial = version_of(self.story)
        response = self.client.post(self.url, {
            'action': 'block_audiobooks', '_selected_action': [self.story.pk], 'index': '0',
        })
        self.assertEqual(response.status_code, 302)
        self.story.refresh_from_db()
        self.other.refresh_from_db()
        self.assertTrue(self.story.audiobook_blacklisted)
        self.assertFalse(self.story.for_recording)
        self.assertFalse(self.other.audiobook_blacklisted)
        self.assertTrue(self.other.for_recording)
        self.assertGreater(version_of(self.story), initial)
        self.assertEqual(LogEntry.objects.filter(object_id=str(self.story.pk), action_flag=CHANGE).count(), 1)
        page = self.client.get(self.url, {'audiobook_blacklisted__exact': '1'})
        self.assertEqual([obj.pk for obj in page.context['cl'].result_list], [self.story.pk])
        response = self.client.post(self.url, {
            'action': 'unblock_audiobooks', '_selected_action': [self.story.pk, self.other.pk], 'index': '0',
        })
        self.assertEqual(response.status_code, 302)
        self.story.refresh_from_db()
        self.other.refresh_from_db()
        self.assertFalse(self.story.audiobook_blacklisted)
        self.assertFalse(self.story.for_recording)
        self.assertTrue(self.other.for_recording)

    def test_staff_cannot_use_blacklist_actions(self):
        staff = get_user_model().objects.create_user('staff-tools', is_staff=True)
        self.client.force_login(staff)
        response = self.client.post(self.url, {'action': 'block_audiobooks', '_selected_action': self.story.pk})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('admin:login'), response.url)
        self.story.refresh_from_db()
        self.assertFalse(self.story.audiobook_blacklisted)

    def test_illustrator_directory_includes_profiles_without_accounts_and_team_membership(self):
        role, _ = Role.objects.get_or_create(name='Ilustrator')
        people = []
        for active in (True, False):
            person = Person.objects.create(first_name='Ilustrator', last_name=str(active),
                                           email=f'{active}@example.test', is_active=False,
                                           illustrator_active=active)
            person.roles.add(role)
            people.append(person)
        Person.objects.create(first_name='Inna', last_name='Osoba', email='inna@example.test')
        for value, expected in (('active', [people[0].pk]), ('inactive', [people[1].pk]),
                                ('all', [p.pk for p in people])):
            page = self.client.get(reverse('admin:people_person_changelist'), {'illustrator_directory': value})
            self.assertEqual(page.status_code, 200)
            self.assertCountEqual([p.pk for p in page.context['cl'].result_list], expected)
            self.assertIn('illustrator_active', page.context['cl'].list_display)

    def test_shortcuts_resolve_and_admin_does_not_load_client_table_pagination(self):
        request = RequestFactory().get(reverse('admin:index'))
        request.user = self.user
        groups = admin.site.get_app_list(request)
        keys = {'audiobooks_queue', 'audiobooks_blacklist', 'audio_description_tasks',
                'illustrators_active', 'illustrators_inactive'}
        entries = [item for group in groups for item in group['models'] if item['object_name'] in keys]
        self.assertEqual(len(entries), len(keys))
        for entry in entries:
            page = self.client.get(entry['admin_url'])
            self.assertEqual(page.status_code, 200, entry)
            self.assertNotIn('e=1', page.request['QUERY_STRING'])
        page = self.client.get(reverse('admin:index'))
        self.assertNotContains(page, 'core/pagination.js')
        self.assertNotContains(page, 'class="cms-pagination"')
        form = self.client.get(reverse('admin:texts_text_change', args=[self.story.pk]))
        self.assertEqual(form.status_code, 200)
        self.assertNotContains(form, 'core/pagination.js')
        self.assertContains(form, 'id="id_audiobook_blacklisted"', count=1)

    def test_task_filter_and_record_page_size(self):
        url = reverse('admin:texts_anthologytask_changelist')
        response = self.client.get(url, {'task_type__exact': 'audio_description', 'status__exact': 'not_commissioned'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['cl'].result_list.values_list('task_type', flat=True)),
                         [AnthologyTask.TaskType.AUDIO_DESCRIPTION])
        response = self.client.get(self.url, {'page_size': '500'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['cl'].list_per_page, 500)
