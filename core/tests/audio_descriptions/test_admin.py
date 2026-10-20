"""Audiodeskrypcja w panelu admina: formularze, historia uwag, wersje formularzy i ochrona antologii przed usunięciem."""
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse
from lxml import html

from core.admin import AnthologyTaskAdminForm
from core.audio_description_admin import AudioDescriptionAdminForm
from core.models import AudioDescription
from fantazmaty.testing import edit_token, form_value
from people.models import Person
from texts.models import Anthology

from .support import AudioDescriptionTestData


class AudioDescriptionAdminTests(AudioDescriptionTestData, TestCase):
    def test_task_status_requires_content_and_matching_assignee(self):
        def errors(**data):
            form = AnthologyTaskAdminForm(data=data, instance=self.task)
            form.is_valid()
            return form.errors

        writer = self.writer.person_profile.pk
        self.assertIn('status', errors(status='ready', assigned_to=writer))  # brak treści AD
        self.assertIn('Zlecone', ' '.join(errors(status='not_commissioned', assigned_to=writer)['status']))
        self.description.content = 'Opis okładki'
        self.description.save()
        missing_person = errors(status='ready')
        self.assertIn('assigned_to', missing_person)
        self.assertNotIn('status', missing_person)
        self.assertNotIn('status', errors(status='ready', assigned_to=writer))

    def test_admin_form_offers_people_without_account(self):
        person = Person.objects.create(first_name='Bez', last_name='Konta', email='bez@example.com')
        form = AudioDescriptionAdminForm(instance=self.description)
        self.assertIn(person, form.fields['controllers'].queryset)

    def test_admin_list_query_count_does_not_grow_with_descriptions(self):
        """Lista audiodeskrypcji z kolumną „Wykonawca i status”: 1 i 4 antologie – ta sama liczba zapytań,
        a kolumna pokazuje wykonawcę i status zadania każdego wiersza."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.task.assigned_to, self.task.status = self.writer.person_profile, 'commissioned'
        self.task.save()
        self.client.force_login(self.admin)
        url = reverse('admin:core_audiodescription_changelist')
        counts = []
        for total in (1, 4):
            for number in range(Anthology.objects.count(), total):
                Anthology.objects.create(title=f'Kolejna antologia {number}')
            self.client.get(url)  # rozgrzewka: sesja i pamięć podręczna
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            counts.append(len(queries))
        self.assertEqual(counts[0], counts[1])
        self.assertContains(response, f'{self.writer.person_profile} – Zlecone')
        self.assertContains(response, 'Nieprzypisane – Niezlecone', count=3)

    def test_controller_assignment_protects_anthology_from_admin_delete(self):
        self.description.controllers.add(self.controller.person_profile)
        request = RequestFactory().get('/')
        request.user = self.admin
        model_admin = admin.site.get_model_admin(Anthology)
        self.assertIn(AudioDescription._meta.verbose_name, model_admin.get_deleted_objects([self.book], request)[2])
        with self.assertRaises(PermissionDenied):
            model_admin.delete_model(request, self.book)
        self.assertTrue(Anthology.objects.filter(pk=self.book.pk).exists())

    def test_admin_controller_change_invalidates_old_forms(self):
        old = edit_token(self.manager, self.book)
        self.client.force_login(self.admin)
        url = reverse('admin:core_audiodescription_change', args=[self.description.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        response = self.client.post(url, {
            'controllers': [self.controller.person_profile.pk], 'stage': 'writing', 'content': '',
            'notes-TOTAL_FORMS': '0', 'notes-INITIAL_FORMS': '0', '_save': 'Zapisz',
            '_edit_version': form_value(page)})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.description.controllers.filter(pk=self.controller.person_profile.pk).exists())
        self.client.force_login(self.manager)
        response = self.client.post(self.detail, {'status': 'not_commissioned', '_edit_version': old})
        self.assertEqual(response.status_code, 409)

    def test_admin_content_stage_and_note_history_and_collapsed_dashboard(self):
        self.assign_writer_and_controller()
        self.post(self.controller, action='note', note='Historia w adminie')
        note = self.description.notes.get()
        self.client.force_login(self.admin)
        url = reverse('admin:core_audiodescription_change', args=[self.description.pk])
        page = self.client.get(url)
        self.assertContains(page, 'Historia w adminie')
        self.assertContains(page, 'data-person-multiple="true"')
        data = {
            '_edit_version': form_value(page), 'stage': 'completed', 'content': 'Treść z admina',
            'controllers': [self.controller.person_profile.pk],
            'notes-TOTAL_FORMS': '1', 'notes-INITIAL_FORMS': '1', 'notes-0-id': str(note.pk),
            'notes-0-description': str(self.description.pk), '_save': 'Zapisz',
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.description.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.description.content, 'Treść z admina')
        self.assertEqual(self.task.status, 'ready')
        dashboard = html.fromstring(self.client.get(reverse('admin:index')).content)
        self.assertTrue(dashboard.xpath('//div[contains(@class,"cms-admin-menu-columns")]'))
        self.assertTrue(dashboard.xpath('//aside[@class="cms-admin-recent"]/details[not(@open)]'))
