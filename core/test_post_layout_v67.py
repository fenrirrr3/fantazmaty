from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.models import PostLayoutAssignment, Audiobook, AudiobookStage, AudioContributor
from core.post_layout import selection_token
from workflow.tests import create_member
from texts.models import Anthology, Text


class PostLayoutEditingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('edit67admin', 'edit67@example.test', 'test')
        cls.manager = create_member('edit67manager', 'Koordynator korekty')
        cls.reader = create_member('edit67reader', 'Korektor poskładowy')
        cls.other = create_member('edit67other', 'Korektor poskładowy')
        cls.book = Anthology.objects.create(title='Przydziały do edycji')
        cls.url = reverse('core:post_layout')

    def item(self, **values):
        return PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.reader,
            page_from=1, page_to=20, assigned_start=date(2026, 1, 1), created_by=self.manager, **values)

    def edit_data(self, item, **values):
        return {'proofreader': self.other.pk, 'page_from': 10, 'page_to': 30,
            'assigned_start': '2026-01-02', 'work_start': '2026-01-04', 'completed_on': '2026-01-06',
            'version': item.version, **values}

    def bulk(self, items, operation, user=None, **values):
        user = user or self.manager
        self.client.force_login(user)
        return self.client.post(self.url, {'action': 'bulk', 'operation': operation,
            'selected': [selection_token(user, item) for item in items], **values})

    def test_edit_completed_dates_and_profile_reassignment(self):
        item = self.item(status='completed', assigned_end=date(2026, 1, 3), work_start=date(2026, 1, 3),
            work_end=date(2026, 1, 5), completed_on=date(2026, 1, 5))
        self.client.force_login(self.manager)
        url = reverse('core:post_layout_edit', args=[item.pk])
        page = self.client.get(url)
        self.assertContains(page, 'type="date"')
        self.assertEqual(self.client.post(url, self.edit_data(item)).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.proofreader, self.other)
        self.assertEqual((item.page_from, item.page_to), (10, 30))
        self.assertEqual(item.assigned_end, date(2026, 1, 4))
        self.assertEqual(item.assigned_end, item.work_start)
        self.assertEqual(item.work_end, item.completed_on)
        self.assertEqual(item.completed_on, date(2026, 1, 6))
        self.assertEqual(item.version, 2)
        for user, count in ((self.reader, 0), (self.other, 1)):
            page = self.client.get(reverse('core:person_detail', args=[user.person_profile.pk]))
            self.assertEqual(len(page.context['assignments']), count)

    def test_edit_guards_and_disabled_future_dates(self):
        item = self.item()
        url = reverse('core:post_layout_edit', args=[item.pk])
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.edit_data(item)).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(url, self.edit_data(item, page_to=1)).status_code, 400)
        self.assertEqual(self.client.post(url, self.edit_data(item)).status_code, 302)
        self.assertEqual(self.client.post(url, self.edit_data(item)).status_code, 409)
        item.refresh_from_db()
        self.assertIsNone(item.work_start)
        self.assertIsNone(item.completed_on)
        self.assertEqual(item.status, 'assigned')
        item.status = 'completed'
        item.assigned_end = item.work_start = date(2026, 1, 4)
        item.work_end = item.completed_on = date(2026, 1, 6)
        item.save()
        self.assertEqual(self.client.post(url, self.edit_data(item, completed_on='2026-01-01')).status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.completed_on, date(2026, 1, 6))

    def test_bulk_reassignment_and_status_transaction(self):
        one = self.item()
        two = self.item(status='in_progress', assigned_end=date(2026, 1, 2), work_start=date(2026, 1, 2))
        self.assertEqual(self.bulk([one, two], 'assign', bulk_proofreader=self.other.pk).status_code, 302)
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual(one.proofreader, self.other)
        self.assertEqual(two.proofreader, self.other)
        # One record cannot skip a step; the whole operation is rolled back.
        self.assertEqual(self.bulk([one, two], 'status', bulk_status='completed').status_code, 400)
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual((one.status, two.status), ('assigned', 'in_progress'))
        self.assertEqual(self.bulk([one, two], 'status', bulk_status='in_progress').status_code, 302)
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual(self.bulk([one, two], 'status', bulk_status='completed').status_code, 302)
        for item in (one, two):
            item.refresh_from_db()
            self.assertEqual(item.status, 'completed')
            self.assertEqual(item.work_end, item.completed_on)
        one.status = 'assigned'
        one.assigned_end = one.work_start = one.work_end = one.completed_on = None
        one.save()
        # The first row advances and the completed second row steps back: both are valid single steps.
        self.assertEqual(self.bulk([one, two], 'status', bulk_status='in_progress').status_code, 302)
        two.refresh_from_db()
        self.assertEqual(two.status, 'in_progress')
        self.assertIsNone(two.completed_on)

    def test_bulk_stale_and_deletion_permissions(self):
        item = self.item()
        stale = selection_token(self.manager, item)
        self.assertEqual(self.bulk([item], 'assign', bulk_proofreader=self.other.pk).status_code, 302)
        self.assertEqual(self.client.post(self.url, {'action': 'bulk', 'operation': 'delete', 'selected': [stale], 'confirm_delete': '1'}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'action': 'bulk', 'operation': 'assign', 'selected': [stale], 'bulk_proofreader': self.reader.pk}).status_code, 409)
        item.refresh_from_db()
        self.assertEqual(self.bulk([item], 'delete', user=self.reader, confirm_delete='1').status_code, 403)
        self.assertEqual(self.bulk([item], 'delete', user=self.admin).status_code, 400)
        self.assertTrue(PostLayoutAssignment.objects.filter(pk=item.pk).exists())
        self.assertEqual(self.bulk([item], 'delete', user=self.admin, confirm_delete='1').status_code, 302)
        self.assertFalse(PostLayoutAssignment.objects.present().filter(pk=item.pk).exists())
        self.assertTrue(PostLayoutAssignment.objects.deleted().filter(pk=item.pk).exists())
        profile = self.client.get(reverse('core:person_detail', args=[self.other.person_profile.pk]))
        self.assertFalse(profile.context['assignments'])

    def test_admin_edits_use_same_dates_and_invalidate_web_form(self):
        item = self.item(status='completed', assigned_end=date(2026, 1, 3), work_start=date(2026, 1, 3),
            work_end=date(2026, 1, 5), completed_on=date(2026, 1, 5))
        self.client.force_login(self.admin)
        url = reverse('admin:core_postlayoutassignment_change', args=[item.pk])
        page = self.client.get(url)
        doc = html.fromstring(page.content)
        token = doc.xpath('//input[@name="_edit_version"]/@value')[0]
        self.assertTrue(doc.xpath('//select[@name="proofreader"]'))
        data = self.edit_data(item, _edit_version=token, _save='Zapisz')
        bad = self.client.post(url, {**data, 'completed_on': '2026-01-01'})
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, 'Zakończenie nie może poprzedzać')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(reverse('core:post_layout_edit', args=[item.pk]), self.edit_data(item)).status_code, 409)
        item.refresh_from_db()
        self.assertEqual(item.proofreader, self.other)
        self.assertEqual(item.assigned_end, item.work_start)
        self.assertEqual(item.work_end, item.completed_on)

    def test_selection_and_post_layout_links(self):
        item = self.item()
        self.client.force_login(self.manager)
        page = self.client.get(self.url)
        doc = html.fromstring(page.content)
        self.assertTrue(doc.xpath('//input[@data-select-table]'))
        self.assertTrue(doc.xpath('//input[@name="selected"][@form="post-layout-bulk"]'))
        self.assertTrue(doc.xpath('//option[@value="delete"]'))
        self.assertContains(page, reverse('core:person_detail', args=[self.reader.person_profile.pk]))
        self.assertContains(page, reverse('core:anthology_detail', args=[self.book.pk]))
        self.assertContains(page, reverse('core:post_layout_edit', args=[item.pk]))
        self.assertFalse(doc.xpath('//table//strong'))

    def test_audio_links_and_montage_sorting(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', pseudonym='Pseudonim')
        text = Text.objects.create(title='Test nagrania', anthology=self.book, length=1)
        text.authors.add(author)
        narrator = AudioContributor.objects.create(name='Test Lektor')
        engineer = AudioContributor.objects.create(name='Test Montażysta')
        reader = create_member('edit67audio', 'Korektor audiobooków')
        Audiobook.objects.create(text=text, status='proofreading', proofreader=reader,
            narrator_contact=narrator, narrator_name=narrator.name, engineer_contact=engineer, engineer_name=engineer.name)
        AudiobookStage.objects.create(text=text, performer=reader, stage_type='proofreading')
        self.client.force_login(self.manager)
        for name in ('core:audio_proofreading', 'core:audiobooks'):
            page = self.client.get(reverse(name), {'sort': 'engineer'})
            self.assertEqual(page.status_code, 200)
            if name == 'core:audiobooks':
                self.assertContains(page, 'Montaż')
            for url in (reverse('core:author_detail', args=[author.pk]), reverse('core:anthology_detail', args=[self.book.pk]),
                    reverse('core:person_detail', args=[reader.person_profile.pk])):
                self.assertContains(page, url)
            if name == 'core:audiobooks':
                for person in (narrator, engineer):
                    self.assertContains(page, reverse('core:audio_contributor', args=[person.pk]))
            else:
                self.assertNotContains(page, reverse('core:audio_contributor', args=[narrator.pk]))
                self.assertNotContains(page, reverse('core:audio_contributor', args=[engineer.pk]))
            doc = html.fromstring(page.content)
            self.assertEqual(doc.xpath('//table//strong/text()'), [text.title])
            self.assertContains(page, 'Pseudonim')
            self.assertNotContains(page, 'Jan Autor')
