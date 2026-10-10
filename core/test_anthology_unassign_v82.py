from django.core import signing
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from illustrations.models import Illustrator
from people.models import Person
from texts.models import Anthology, AnthologyTask
from workflow.tests import create_member


class AnthologyUnassignTests(TestCase):
    def setUp(self):
        self.manager = create_member('unassign-manager', 'Koordynator redakcji')
        self.member = create_member('unassign-member', 'Redaktor')
        self.book = Anthology.objects.create(title='Antologia do ponownego przypisania')
        self.url = reverse('core:anthology_detail', args=[self.book.pk])
        self.client.force_login(self.manager)

    def token(self):
        return signing.dumps([self.manager.pk, f'texts.anthology:{self.book.pk}', version_of(self.book)],
                             salt='cms-edit-version')

    def remove(self, kind, **extra):
        return self.client.post(self.url, {
            'action': 'tasks_and_cover', 'remove_task': kind, '_edit_version': self.token(), **extra,
        })

    def add_new(self, kind, name):
        return self.client.post(self.url, {
            'action': 'new_person', '_edit_version': self.token(),
            'new-person-task_type': kind, 'new-person-first_name': name,
            'new-person-last_name': 'Nowa osoba',
        })

    def test_each_task_can_be_cleared_then_assigned_to_new_contact(self):
        for kind in AnthologyTask.TaskType.values:
            with self.subTest(kind=kind):
                task = self.book.production_tasks.get(task_type=kind)
                task.assigned_to = self.member.person_profile
                task.status = 'commissioned'
                task.save()
                self.assertIsNotNone(task.commissioned_at)
                document = html.fromstring(self.client.get(self.url).content)
                self.assertTrue(document.xpath(f'//fieldset[@id="task-{kind}"]//button[@name="remove_task"][@formnovalidate]'))
                self.assertEqual(self.remove(kind).status_code, 302)
                task.refresh_from_db()
                self.assertEqual(task.status, 'not_commissioned')
                self.assertIsNone(task.assigned_to_id)
                self.assertIsNone(task.commissioned_at)
                self.assertTrue(Person.objects.filter(pk=self.member.person_profile.pk).exists())
                self.assertEqual(self.add_new(kind, kind).status_code, 302)
                task.refresh_from_db()
                self.assertEqual(task.status, 'commissioned')
                self.assertEqual(task.assigned_to.first_name, kind)

    def test_legacy_and_linked_cover_clear_completely_keep_notes_and_allow_new_contact(self):
        artist = Illustrator.objects.create(first_name='Dotychczasowy', last_name='Ilustrator')
        for contact in (None, artist):
            with self.subTest(contact=contact):
                self.book.refresh_from_db()
                self.book.cover_author = 'Historyczny podpis'
                self.book.cover_illustrator = contact
                self.book.cover_status = 'ready'
                self.book.cover_notes = 'Zachowaj uwagi'
                self.book.save()
                # A finished cover keeps its credit; removal is offered only before completion.
                self.assertEqual(self.remove('cover').status_code, 302)
                self.book.refresh_from_db()
                self.assertEqual(self.book.cover_status, 'ready')
                self.book.cover_status = 'in_progress'
                self.book.save()
                page = self.client.get(self.url)
                document = html.fromstring(page.content)
                self.assertTrue(document.xpath('//fieldset[@id="task-cover"]//button[@name="remove_task"][@formnovalidate]'))
                self.assertEqual(self.remove('cover').status_code, 302)
                self.book.refresh_from_db()
                self.assertIsNone(self.book.cover_illustrator_id)
                self.assertEqual(self.book.cover_author, '')
                self.assertEqual(self.book.cover_status, 'not_started')
                self.assertIsNone(self.book.cover_commissioned_at)
                self.assertEqual(self.book.cover_notes, 'Zachowaj uwagi')
                self.assertTrue(Illustrator.objects.filter(pk=artist.pk).exists())
                self.assertNotContains(self.client.get(self.url), 'Dotychczasowy zapis:')
                self.assertEqual(self.add_new('cover', f'Nowy{contact is None}').status_code, 302)
                self.book.refresh_from_db()
                self.assertEqual(self.book.cover_status, 'in_progress')
                self.assertTrue(self.book.cover_illustrator_id)

    def test_removal_is_independent_of_unsaved_other_fields(self):
        task = self.book.production_tasks.get(task_type='blurb')
        task.assigned_to = self.member.person_profile
        task.status = 'commissioned'
        task.save()
        self.book.cover_author = 'Stara okładka'
        self.book.cover_status = 'ready'
        self.book.save()
        self.assertEqual(self.remove('blurb', **{
            'cover-cover_status': 'not_started', 'cover-clear_legacy_cover': 'on',
            'banners-status': 'invalid', 'blurb-assigned_to': 'invalid',
        }).status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, 'Stara okładka')
        self.assertEqual(self.book.cover_status, 'ready')
        task.refresh_from_db()
        self.assertEqual(task.status, 'not_commissioned')

    def test_removal_requires_permission_current_version_and_known_task(self):
        self.book.cover_author = 'Nie usuwaj'
        self.book.cover_status = 'ready'
        self.book.save()
        old = self.token()
        self.book.cover_notes = 'Zmiana'
        self.book.save()
        self.assertEqual(self.remove('cover', _edit_version=old).status_code, 409)
        self.assertEqual(self.remove('cover', _edit_version='').status_code, 409)
        self.assertEqual(self.remove('unknown').status_code, 400)
        self.client.force_login(self.member)
        self.assertEqual(self.remove('cover').status_code, 403)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_author, 'Nie usuwaj')
