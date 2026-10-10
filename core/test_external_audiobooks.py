from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.audiobook_validators import validate_mega_url
from core.edit_versions import version_of
from core.models import PublicAudiobookSettings
from people.models import Person, Role
from texts.models import Anthology, Text
from workflow.models import WorkflowStage


class ExternalAudiobookTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('audio-public-admin', 'admin@example.test', 'test')
        cls.member = get_user_model().objects.create_user('audio-public-member', is_staff=True)
        person = Person.objects.create(user=cls.member, first_name='Jan', last_name='Test')
        person.roles.add(Role.objects.get_or_create(name='Koordynator')[0])
        cls.book = Anthology.objects.create(title='Antologia gotowa', status=Anthology.Status.READY)
        cls.story = Text.objects.create(anthology=cls.book, title='Opowiadanie do nagrania', length=100,
            tags='kosmos, podróż', genre='science fiction', coordinator_note='PRYWATNE UWAGI',
            file_url='https://example.test/private-folder')
        author = Author.objects.create(first_name='Prywatne', last_name='Nazwisko', email='private@example.test')
        cls.story.authors.add(author)
        cls.url = reverse('core:external_audiobooks')
        cls.download = reverse('core:audiobook_guidelines_txt')

    def test_public_projection_is_read_only_and_internal_pages_stay_protected(self):
        for login in (False, True):
            if login:
                self.client.force_login(self.admin)
            response = self.client.get(self.url, {'sort': 'coordinator_note'})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, self.story.title)
            self.assertEqual(set(response.context['rows'][0]), {'anthology__title', 'title', 'tags', 'genre', 'public_status', 'audio_status', 'audiobook__narrator_name'})
            for value in ('PRYWATNE UWAGI', 'private-folder', 'private@example.test', 'Prywatne', 'site-sidebar'):
                self.assertNotContains(response, value)
            self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
            self.assertIn('no-store', response['Cache-Control'])
            self.assertEqual(self.client.post(self.url, {}).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('core:audiobooks')).status_code, 302)
        self.assertEqual(self.client.get(reverse('core:assigned_text_detail', args=[self.story.pk])).status_code, 302)
        self.assertFalse(PublicAudiobookSettings.objects.exists())

    def test_all_scope_conditions_and_visibility_after_changes(self):
        preparing = Anthology.objects.create(title='Antologia w przygotowaniu')
        novel = Anthology.objects.create(title='Powieść gotowa', is_novel=True)
        # A legacy novel marked ready must still be outside the audio scope.
        Anthology.objects.filter(pk=novel.pk).update(status=Anthology.Status.READY)
        for name, fields in [('Brak zgody', {'for_recording': False}), ('Czarna lista', {'audiobook_blacklisted': True}),
                             ('W przygotowaniu', {'anthology': preparing}), ('Bez antologii', {'anthology': None}),
                             ('Rozdział', {'anthology': novel, 'chapter_number': 1}), ('Wycofany', {})]:
            values = {'anthology': self.book, 'title': name, 'length': 100, **fields}
            story = Text.objects.create(**values)
            if name == 'Wycofany':
                WorkflowStage.objects.create(text=story, stage_type='withdrawn')
            if name == 'Rozdział':
                Text.objects.filter(pk=story.pk).update(for_recording=True)
        # Even inconsistent legacy data with both flags set must stay private.
        Text.objects.filter(title='Czarna lista').update(for_recording=True)
        response = self.client.get(self.url)
        self.assertEqual([row['title'] for row in response.context['rows']], [self.story.title])
        self.assertEqual(list(response.context['anthologies']), [{'anthology_id': self.book.pk, 'anthology__title': self.book.title}])
        self.story.for_recording = False
        self.story.save(update_fields=['for_recording'])
        self.assertEqual(self.client.get(self.url).context['page_obj'].paginator.count, 0)

    def test_historical_withdrawal_does_not_hide_current_cycle(self):
        WorkflowStage.objects.create(text=self.story, stage_type='withdrawn', workflow_cycle=1, is_current=False)
        self.story.current_workflow_cycle = 2
        self.story.save(update_fields=['current_workflow_cycle'])
        self.assertContains(self.client.get(self.url), self.story.title)

    def test_filters_sorting_and_pagination_cover_only_eligible_texts(self):
        Text.objects.bulk_create([Text(title=f'Tekst {i:02}', anthology=self.book, length=1, tags='smoki', genre='fantasy') for i in range(30)])
        response = self.client.get(self.url, {'q': 'smoki', 'anthology': self.book.pk, 'sort': '-title', 'page': 2})
        self.assertEqual(response.context['page_obj'].paginator.count, 30)
        self.assertEqual([r['title'] for r in response.context['rows']], [f'Tekst {i:02}' for i in range(4, -1, -1)])
        self.assertIn('q=smoki', response.context['page_obj'].first_url)
        for invalid in ('-1', '1 OR 1=1', '9' * 100, '１２'):
            self.assertEqual(self.client.get(self.url, {'anthology': invalid}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.url, {'status': 'Wycofany'}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.url, {'q': 'PODRÓŻ'}).context['page_obj'].paginator.count, 1)
        for sort in ('anthology', 'title', 'tags', 'genre', 'status', 'authors__email'):
            self.assertEqual(self.client.get(self.url, {'sort': '-' + sort}).status_code, 200)

    def test_guidelines_escape_html_and_download_original_utf8_text(self):
        self.assertContains(self.client.get(self.url), 'https://mega.nz/')
        self.assertContains(self.client.get(self.url), 'Wytyczne zostaną uzupełnione.')
        self.assertEqual(self.client.get(self.download).status_code, 404)
        content = 'Wytyczne: zażółć gęślą jaźń.\n<script>alert(1)</script>\nDrugi akapit.'
        PublicAudiobookSettings.objects.create(mega_url='https://mega.nz/folder/example#key', guidelines=content)
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, 'https://mega.nz/folder/example#key')
        doc = html.fromstring(response.content)
        self.assertFalse(doc.xpath('//details[@class="audiobook-guidelines"][@open]'))
        response = self.client.get(self.download)
        self.assertEqual(response.content.decode('utf-8'), content)
        self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertEqual(self.client.post(self.download, {}).status_code, 405)

    def test_only_safe_mega_urls_are_accepted_and_rendered(self):
        for url in ('javascript:alert(1)', 'http://mega.nz/', 'https://mega.nz.evil.test/', 'https://mega.nz@evil.test/', 'https://user@mega.nz/', 'https://mega.nz:8443/'):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                validate_mega_url(url)
        config = PublicAudiobookSettings.objects.create()
        PublicAudiobookSettings.objects.filter(pk=config.pk).update(mega_url='javascript:alert(1)')
        self.assertNotContains(self.client.get(self.url), 'javascript:')
        with transaction.atomic(), self.assertRaises(IntegrityError):
            PublicAudiobookSettings.objects.create(pk=2)

    def test_admin_settings_permissions_versioning_and_navigation(self):
        config = PublicAudiobookSettings.objects.create()
        url = reverse('admin:core_publicaudiobooksettings_change', args=[1])
        self.client.force_login(self.member)
        self.assertIn(self.client.get(url).status_code, (302, 403))
        self.assertNotContains(self.client.get(reverse('core:audiobooks')), 'MegaNZ i wytyczne – ustawienia')
        self.client.force_login(self.admin)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        data = {node.get('name'): node.get('value', '') for node in doc.xpath('//form[@id="publicaudiobooksettings_form"]//input[@type="hidden"][@name]')}
        data.update(mega_url='https://mega.nz/folder/demo#key', guidelines='Nowe wytyczne', _save='Zapisz')
        before = version_of(config)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        config.refresh_from_db()
        self.assertEqual(config.guidelines, 'Nowe wytyczne')
        self.assertNotEqual(version_of(config), before)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        doc = html.fromstring(self.client.get(reverse('core:audiobooks')).content)
        link = doc.xpath(f'//a[@href="{self.url}"]')[0]
        self.assertEqual(link.get('target'), '_blank')
        self.assertIn('noopener', link.get('rel'))
        self.assertNotContains(self.client.get(reverse('core:home')), self.url)
        index = html.fromstring(self.client.get(reverse('admin:index')).content)
        self.assertTrue(index.xpath('//*[@id="group-audio"]//a[contains(@href,"zewnetrzne-audiobooki")]'))
