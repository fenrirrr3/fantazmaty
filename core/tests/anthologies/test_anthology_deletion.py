"""Usuwanie antologii w adminie: podgląd bez zmian, puste zadania usuwane razem z antologią, przypisane zadania i teksty blokują usunięcie."""
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.test import TestCase, RequestFactory
from django.urls import reverse

from texts.models import Anthology, AnthologyTask, Text
from fantazmaty.testing import create_member, create_superuser, form_value


class AnthologyDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_superuser('delete-admin')

    def request(self):
        request = RequestFactory().get('/')
        request.user = self.user
        return request

    def test_delete_preview_does_not_mutate_and_single_delete_removes_empty_tasks(self):
        book = Anthology.objects.create(title='Pusta do usunięcia')
        tasks = list(book.production_tasks.values_list('pk', flat=True))
        self.client.force_login(self.user)
        url = reverse('admin:texts_anthology_delete', args=[book.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context['perms_lacking'])
        self.assertTrue(AnthologyTask.objects.filter(pk__in=tasks).exists())
        token = form_value(page)
        self.assertEqual(self.client.post(url, {'post': 'yes', '_edit_version': token}).status_code, 302)
        self.assertFalse(Anthology.objects.filter(pk=book.pk).exists())
        self.assertFalse(AnthologyTask.objects.filter(pk__in=tasks).exists())

    def test_assigned_or_commissioned_task_protects_parent_and_bulk_is_atomic(self):
        model_admin = admin.site.get_model_admin(Anthology)
        person = create_member('delete-typesetter', 'Składacz').person_profile
        empty = Anthology.objects.create(title='Pusta zachowana')
        book = Anthology.objects.create(title='Z przypisaniem')
        task = book.production_tasks.first()
        for status, assignee in [('not_commissioned', person), ('commissioned', person), ('ready', person)]:
            task.status, task.assigned_to = status, assignee
            task.save()
            request = self.request()
            _, _, permissions, _ = model_admin.get_deleted_objects([book], request)
            self.assertIn(AnthologyTask._meta.verbose_name, permissions)
            self.assertFalse(hasattr(request, '_deleting_anthologies'))
            with self.assertRaises(PermissionDenied):
                model_admin.delete_queryset(request, Anthology.objects.filter(pk__in=[empty.pk, book.pk]))
            self.assertEqual(Anthology.objects.filter(pk__in=[empty.pk, book.pk]).count(), 2)
        self.assertFalse(admin.site.get_model_admin(AnthologyTask).has_delete_permission(self.request(), task))
        task.status, task.assigned_to = 'not_commissioned', None
        task.save()
        self.assertFalse(admin.site.get_model_admin(AnthologyTask).has_delete_permission(self.request(), task))
        model_admin.delete_queryset(self.request(), Anthology.objects.filter(pk__in=[empty.pk, book.pk]))
        self.assertEqual(Anthology.objects.filter(pk__in=[empty.pk, book.pk]).count(), 0)

    def test_other_protected_records_still_block_delete(self):
        book = Anthology.objects.create(title='Antologia z tekstem')
        Text.objects.create(anthology=book, title='Zachowaj mnie', length=1)
        model_admin = admin.site.get_model_admin(Anthology)
        self.assertTrue(model_admin.get_deleted_objects([book], self.request())[3])
        with self.assertRaises(PermissionDenied):
            model_admin.delete_model(self.request(), book)
        self.assertTrue(Anthology.objects.filter(pk=book.pk).exists())


