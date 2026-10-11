"""Czarna lista audiobooków i zgoda na nagranie: blokada tekstu i całej antologii, odblokowanie tylko w panelu admina."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from fantazmaty.testing import create_member, create_superuser, edit_token
from texts.models import Anthology, Text


class TextBlacklistTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('audio-member', 'Lektor')
        cls.admin = create_superuser('audio-admin')
        cls.book = Anthology.objects.create(title='Audio')
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email=None)
        cls.text = Text.objects.create(title='Nagranie', length=100, anthology=cls.book)
        cls.text.authors.add(cls.author)
        cls.url = reverse('core:update_text_audiobook', args=[cls.text.pk])
        cls.detail = reverse('core:assigned_text_detail', args=[cls.text.pk])

    def setUp(self):
        self.client.force_login(self.member)

    def token(self, user=None):
        self.text.refresh_from_db()
        return edit_token(user or self.member, self.text)

    def queue_ids(self):
        response = self.client.get(reverse('core:audiobooks'))
        self.assertEqual(response.status_code, 200)
        return {row['pk'] for row in response.context['texts']}

    def block(self):
        response = self.client.post(self.url, {'for_recording': 'on', 'audiobook_blacklisted': 'on',
                                               '_edit_version': self.token()})
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
                data = {'for_recording': 'on', '_edit_version': self.token(user)}
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
        form = form_class({'title': self.text.title, 'length': 100, 'anthology': self.book.pk,
                           'authors': [self.author.pk]}, instance=self.text)
        self.assertTrue(form.is_valid(), form.errors)
        model_admin.save_model(request, form.save(commit=False), form, change=True)
        self.text.refresh_from_db()
        self.assertFalse(self.text.audiobook_blacklisted)
        self.assertFalse(self.text.for_recording)
        self.assertEqual(self.client.post(self.url, {'for_recording': 'on', '_edit_version': self.token()}).status_code, 302)
        self.assertIn(self.text.pk, self.queue_ids())

    def test_stale_or_missing_version_cannot_override_new_block(self):
        stale = self.token()
        self.block()
        self.assertEqual(self.client.post(self.url, {'for_recording': 'on', '_edit_version': stale}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'for_recording': 'on'}).status_code, 409)
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
        response = self.client.post(self.url, {'audiobook_blacklisted': 'on', '_edit_version': self.token(outsider)})
        self.assertEqual(response.status_code, 403)
        self.text.refresh_from_db()
        self.assertFalse(self.text.audiobook_blacklisted)


class AnthologyRecordingBlockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = create_member('block-manager', 'Koordynator redakcji')
        cls.book = Anthology.objects.create(title='Antologia do zablokowania')
        cls.texts = [Text.objects.create(title=f'Opowiadanie {i}', anthology=cls.book, length=1) for i in range(3)]
        cls.url = reverse('core:anthology_detail', args=[cls.book.pk])

    def setUp(self):
        self.client.force_login(self.manager)

    def test_button_blocks_every_text_but_not_the_audio_description(self):
        page = html.fromstring(self.client.get(self.url).content)
        self.assertTrue(page.xpath('//button[@data-dialog-open="block-recording"][normalize-space()="Zablokuj nagrywanie"]'))
        task = self.book.production_tasks.get(task_type='audio_description')
        before = task.status
        response = self.client.post(self.url, {'action': 'block_recording',
                                               '_edit_version': edit_token(self.manager, self.book)})
        self.assertEqual(response.status_code, 302)
        for text in self.texts:
            text.refresh_from_db()
            self.assertTrue(text.audiobook_blacklisted)
            self.assertFalse(text.for_recording)
        task.refresh_from_db()
        self.assertEqual(task.status, before)
        self.assertContains(self.client.get(self.url), 'Wszystkie teksty są na czarnej liście audiobooków.')

    def test_block_requires_a_form_version(self):
        self.assertEqual(self.client.post(self.url, {'action': 'block_recording'}).status_code, 409)
        self.assertFalse(Text.objects.filter(audiobook_blacklisted=True).exists())
