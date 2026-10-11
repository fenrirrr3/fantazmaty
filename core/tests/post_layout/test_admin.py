"""Korekta poskładowa w panelu admina: te same reguły dat co na stronie, wersje formularzy, miękkie usuwanie i ograniczenia bazy."""
from django.contrib.admin.sites import site
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from lxml import html

from core.admin import PostLayoutAssignmentAdmin
from core.models import PostLayoutAssignment
from core.post_layout import AssignmentForm

from .support import PostLayoutTestData


class PostLayoutAdminTests(PostLayoutTestData, TestCase):
    def test_admin_page_and_database_guards(self):
        item = self.create()
        self.client.force_login(self.admin)
        page = self.client.get(reverse('admin:core_postlayoutassignment_change', args=[item.pk]))
        self.assertContains(page, 'Otwórz Korektę poskładową')
        self.assertNotContains(page, 'name="status"')
        self.assertContains(self.client.get(reverse('admin:index')), 'Korekta poskładowa')
        with self.assertRaises(IntegrityError), transaction.atomic():
            PostLayoutAssignment.objects.filter(pk=item.pk).update(page_to=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PostLayoutAssignment.objects.filter(pk=item.pk).update(status='completed')
        person = self.reader.person_profile
        person.is_external = True
        person.save()
        self.assertFalse(AssignmentForm(self.form_data(), user=self.manager).is_valid())

    def test_admin_edits_use_same_dates_and_invalidate_web_form(self):
        item = self.completed_item()
        self.client.force_login(self.admin)
        url = reverse('admin:core_postlayoutassignment_change', args=[item.pk])
        doc = html.fromstring(self.client.get(url).content)
        token = doc.xpath('//input[@name="_edit_version"]/@value')[0]
        self.assertTrue(doc.xpath('//select[@name="proofreader"]'))
        data = self.edit_data(item, _edit_version=token, _save='Zapisz')
        bad = self.client.post(url, {**data, 'completed_on': '2026-01-01'})
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, 'Zakończenie nie może poprzedzać')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.client.force_login(self.manager)
        edit = reverse('core:post_layout_edit', args=[item.pk])
        self.assertEqual(self.client.post(edit, self.edit_data(item)).status_code, 409)
        item.refresh_from_db()
        self.assertEqual(item.proofreader, self.other)
        self.assertEqual(item.assigned_end, item.work_start)
        self.assertEqual(item.work_end, item.completed_on)

    def test_admin_delete_keeps_history_and_locks_entry(self):
        item = self.item()
        request = RequestFactory().post('/')
        request.user = self.admin
        model_admin = PostLayoutAssignmentAdmin(PostLayoutAssignment, site)
        model_admin.delete_model(request, item)
        item.refresh_from_db()
        self.assertIsNotNone(item.deleted_at)
        self.assertFalse(model_admin.has_change_permission(request, item))
