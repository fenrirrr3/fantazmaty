from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from people.models import Person
from texts.models import Anthology, Text


@override_settings(DROPBOX_CHOOSER_APP_KEY='test-public-app-key')
class DropboxChooserTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.admin = users.objects.create_superuser('chooser-admin', 'chooser-admin@example.com', 'test')
        self.member = users.objects.create_user('chooser-member', 'chooser-member@example.com', 'test')
        Person.objects.create(user=self.member, first_name='Jan', last_name='Test', email=self.member.email)
        self.book = Anthology.objects.create(title='Test wyboru folderu')
        self.text = Text.objects.create(title='Folder', length=100, anthology=self.book,
                                       file_url='https://www.dropbox.com/sh/existing/link?dl=0')
        self.url = reverse('core:assigned_text_detail', args=[self.text.pk])

    def test_superuser_sees_picker_and_get_does_not_change_link(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertContains(response, 'id="dropbox-folder-choose"')
        self.assertContains(response, 'data-app-key="test-public-app-key"')
        self.assertContains(response, 'dropbox-folder-chooser.js')
        self.assertContains(response, 'Zapisz link')
        from lxml import html
        page = html.fromstring(response.content)
        buttons = page.xpath('//*[@id="dropbox-folder-choose"]/parent::div/button')
        self.assertEqual([button.text_content().strip() for button in buttons], ['Wybierz folder Dropbox', 'Zapisz link'])
        self.text.refresh_from_db()
        self.assertEqual(self.text.file_url, 'https://www.dropbox.com/sh/existing/link?dl=0')

    def test_member_sees_link_but_no_picker_or_app_key(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url)
        self.assertContains(response, 'Otwórz folder Dropbox')
        self.assertNotContains(response, 'dropbox-folder-choose')
        self.assertNotContains(response, 'test-public-app-key')
        self.assertNotContains(response, 'dropins.js')
        response = self.client.post(reverse('core:update_text_file', args=[self.text.pk]),
                                    {'file_url':'https://www.dropbox.com/sh/changed/link'})
        self.assertEqual(response.status_code, 403)
        self.text.refresh_from_db()
        self.assertIn('/existing/', self.text.file_url)

    @override_settings(DROPBOX_CHOOSER_APP_KEY='')
    def test_unconfigured_picker_has_manual_fallback_and_no_external_script(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertContains(response, 'data-input-id="id_file_url" disabled')
        self.assertContains(response, 'Link nadal możesz wkleić ręcznie')
        self.assertNotContains(response, 'dropins.js')

    def test_ready_anthology_hides_picker_and_external_script(self):
        from workflow.models import WorkflowStage
        from django.utils import timezone
        today = timezone.localdate()
        WorkflowStage.objects.create(text=self.text, stage_type='ready', is_completed=True, started_at=today, ended_at=today)
        self.book.status = 'ready'; self.book.save()
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'dropbox-folder-choose')
        self.assertNotContains(response, 'dropins.js')
