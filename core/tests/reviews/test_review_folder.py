"""Folder Dropbox zgłoszenia: wybór i zapis przez uprawnionych, ukrycie przed recenzentem, przeniesienie do tekstu."""
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from lxml import html as parser

from core.services.reviews import copy_review_to_text
from fantazmaty.testing import create_member, create_superuser, form_value
from texts.admin import review_text_initial
from texts.models import Anthology, Review


@override_settings(DROPBOX_CHOOSER_APP_KEY='public-test-key')
class ReviewFolderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin-folder')
        cls.member = create_member('member-folder', 'Recenzent')
        cls.book = Anthology.objects.create(title='Nabór folderów')

    def setUp(self):
        self.review = Review.objects.create(
            title='Zgłoszenie', anthology=self.book, author_first_name='Jan', author_last_name='Autor',
            email='author@example.com', length=1234, status='accepted', author_notified_at=timezone.localdate(),
            old_reviews=True, file_url='https://www.dropbox.com/sh/original/link')
        self.detail = reverse('core:assigned_review_detail', args=[self.review.pk])
        self.save = reverse('core:update_review_file', args=[self.review.pk])

    def token(self, detail=None):
        return form_value(self.client.get(detail or self.detail))

    def test_superuser_picker_and_save_clear_invalid_url(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.detail)
        self.assertContains(response, 'data-app-key="public-test-key"')
        self.assertContains(response, 'review-folder-layout')
        page = parser.fromstring(response.content)
        self.assertEqual(len(page.xpath('//*[@class="review-folder-layout"]/section')), 2)
        self.assertEqual(len(page.xpath('//*[@id="dropbox-folder-choose"]/parent::div/button')), 2)
        response = self.client.post(self.save, {'file_url': 'https://www.dropbox.com/sh/new/link',
                                                '_edit_version': self.token()})
        self.assertEqual(response.status_code, 302)
        self.review.refresh_from_db()
        self.assertIn('/new/', self.review.file_url)
        response = self.client.post(self.save, {'file_url': 'javascript:alert(1)', '_edit_version': self.token()})
        self.assertContains(response, 'Wybierz folder Dropbox', status_code=400)
        self.review.refresh_from_db()
        self.assertIn('/new/', self.review.file_url)
        response = self.client.post(self.save, {'file_url': '', '_edit_version': self.token()})
        self.assertEqual(response.status_code, 302)
        self.review.refresh_from_db()
        self.assertEqual(self.review.file_url, '')

    def test_coordinator_manages_folder_without_author_identity(self):
        coordinator = create_member('audit-coord', 'Koordynator recenzji')
        review = Review.objects.create(
            title='Zgłoszenie', anthology=self.book, length=1000, author_first_name='Tajny',
            author_last_name='Autor', email='secret@example.test', author_message='SECRET_MESSAGE',
            file_url='https://www.dropbox.com/secret-folder')
        detail = reverse('core:assigned_review_detail', args=[review.pk])
        self.client.force_login(coordinator)
        page = self.client.get(detail)
        self.assertContains(page, 'review-folder-heading')
        for secret in ('secret@example.test', 'SECRET_MESSAGE', 'Tajny Autor'):
            self.assertNotContains(page, secret)
        result = self.client.post(reverse('core:update_review_file', args=[review.pk]), {
            'file_url': 'https://www.dropbox.com/changed-folder', '_edit_version': self.token(detail)})
        self.assertEqual(result.status_code, 302)
        review.refresh_from_db()
        self.assertIn('changed-folder', review.file_url)

    def test_member_neither_sees_folder_nor_can_save(self):
        self.client.force_login(self.member)
        response = self.client.get(self.detail)
        self.assertEqual(response.status_code, 200)
        for value in ('dropins.js', 'public-test-key', '/original/link', 'review-folder-heading',
                      'author@example.com'):
            self.assertNotContains(response, value)
        self.assertEqual(self.client.post(self.save, {'file_url': ''}).status_code, 403)
        self.review.refresh_from_db()
        self.assertIn('/original/', self.review.file_url)

    def test_stale_or_missing_token_does_not_overwrite_folder(self):
        self.client.force_login(self.admin)
        token = self.token()
        self.review.title = 'Zmieniony tytuł'
        self.review.save()
        for data in ({'file_url': '', '_edit_version': token}, {'file_url': ''}):
            self.assertEqual(self.client.post(self.save, data).status_code, 409)
        self.review.refresh_from_db()
        self.assertIn('/original/', self.review.file_url)

    def test_transfer_and_admin_popup_carry_folder(self):
        self.assertEqual(review_text_initial(self.review)['file_url'], self.review.file_url)
        text = copy_review_to_text(user=self.admin, review_id=self.review.pk, contract_received=True)
        self.assertEqual(text.file_url, self.review.file_url)
        self.assertEqual(copy_review_to_text(user=self.admin, review_id=self.review.pk).pk, text.pk)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:assigned_text_detail', args=[text.pk]))
        self.assertContains(
            page, 'class="secondary-button"\n                    >\n                        Otwórz zgłoszenie')
        self.assertContains(page, text.file_url)

    @override_settings(DROPBOX_CHOOSER_APP_KEY='')
    def test_manual_fallback_without_key(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.detail)
        self.assertContains(response, 'data-input-id="id_file_url" disabled')
        self.assertNotContains(response, 'dropins.js')
