from tempfile import TemporaryDirectory

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import RequestFactory, TestCase
from django.urls import reverse

from authors.models import Author
from core.edit_versions import version_of
from texts.models import Anthology, Text
from workflow.tests import create_member


class AudiobookBlacklistTests(TestCase):
    def setUp(self):
        spool = TemporaryDirectory()
        self.addCleanup(spool.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=spool.name)
        override.enable()
        self.addCleanup(override.disable)
        self.member = create_member('audio-member', 'Redaktor')
        self.admin = get_user_model().objects.create_superuser('audio-admin', 'audio@example.test', 'test')
        self.book = Anthology.objects.create(title='Audio')
        self.author = Author.objects.create(first_name='Jan', last_name='Autor', email=None)
        self.text = Text.objects.create(title='Nagranie', length=100, anthology=self.book)
        self.text.authors.add(self.author)
        self.url = reverse('core:update_text_audiobook', args=[self.text.pk])
        self.detail = reverse('core:assigned_text_detail', args=[self.text.pk])
        self.client.force_login(self.member)

    def token(self, user=None):
        self.text.refresh_from_db()
        return signing.dumps([(user or self.member).pk, f'texts.text:{self.text.pk}', version_of(self.text)], salt='cms-edit-version')

    def queue_ids(self):
        response = self.client.get(reverse('core:audiobooks'))
        self.assertEqual(response.status_code, 200)
        return {row['pk'] for row in response.context['texts']}

    def block(self):
        response = self.client.post(self.url, {'for_recording':'on', 'audiobook_blacklisted':'on', '_edit_version':self.token()})
        self.assertEqual(response.status_code, 302)
        self.text.refresh_from_db()

    def test_block_removes_recording_and_queue_entry(self):
        self.assertFalse(self.text.audiobook_blacklisted)
        self.assertIn(self.text.pk, self.queue_ids())
        self.block()
        self.assertTrue(self.text.audiobook_blacklisted)
        self.assertFalse(self.text.for_recording)
        self.assertNotIn(self.text.pk, self.queue_ids())
        response = self.client.get(self.detail)
        self.assertTrue(response.context['audiobook_form'].fields['for_recording'].disabled)
        self.assertTrue(response.context['audiobook_form'].fields['audiobook_blacklisted'].disabled)
        self.assertContains(response, 'wyłącznie w panelu admina')

    def test_forged_web_posts_cannot_unblock_even_for_superuser(self):
        self.block()
        for user in (self.member, self.admin):
            self.client.force_login(user)
            for value in (None, '', 'false'):
                data = {'for_recording':'on', '_edit_version':self.token(user)}
                if value is not None:
                    data['audiobook_blacklisted'] = value
                self.assertEqual(self.client.post(self.url, data).status_code, 302)
                self.text.refresh_from_db()
                self.assertTrue(self.text.audiobook_blacklisted)
                self.assertFalse(self.text.for_recording)

    def test_admin_form_can_unblock_then_web_can_enable_recording(self):
        self.block()
        request = RequestFactory().get('/panel/')
        request.user = self.admin
        model_admin = admin.site._registry[Text]
        form_class = model_admin.get_form(request, obj=self.text)
        self.assertFalse(form_class.base_fields['audiobook_blacklisted'].disabled)
        form = form_class({'title':self.text.title, 'length':100, 'anthology':self.book.pk, 'authors':[self.author.pk]}, instance=self.text)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        model_admin.save_model(request, obj, form, change=True)
        self.text.refresh_from_db()
        self.assertFalse(self.text.audiobook_blacklisted)
        self.assertFalse(self.text.for_recording)
        self.assertEqual(self.client.post(self.url, {'for_recording':'on', '_edit_version':self.token()}).status_code, 302)
        self.assertIn(self.text.pk, self.queue_ids())

    def test_stale_form_cannot_override_new_block(self):
        stale = self.token()
        self.block()
        self.assertEqual(self.client.post(self.url, {'for_recording':'on', '_edit_version':stale}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'for_recording':'on'}).status_code, 409)
        self.text.refresh_from_db()
        self.assertTrue(self.text.audiobook_blacklisted)
        self.assertFalse(self.text.for_recording)

    def test_model_save_and_queue_defend_against_inconsistent_flags(self):
        self.text.audiobook_blacklisted = True
        self.text.save(update_fields=['audiobook_blacklisted'])
        self.text.refresh_from_db()
        self.assertFalse(self.text.for_recording)
        Text.objects.filter(pk=self.text.pk).update(for_recording=True)
        self.assertNotIn(self.text.pk, self.queue_ids())

    def test_unauthorized_user_cannot_change_flags(self):
        outsider = get_user_model().objects.create_user('audio-outsider')
        self.client.force_login(outsider)
        self.assertEqual(self.client.post(self.url, {'audiobook_blacklisted':'on', '_edit_version':self.token(outsider)}).status_code, 403)
        self.text.refresh_from_db()
        self.assertFalse(self.text.audiobook_blacklisted)
