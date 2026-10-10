from datetime import date

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.edit_versions import version_of
from core.models import Audiobook
from texts.models import Anthology, Text, TextTranslation, ForeignAuthor
from workflow.models import WorkflowStage
from workflow.tests import create_member


class AudiobookProductionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('audio-manager', 'audio@example.test', 'test')
        cls.coordinator = create_member('audio-coordinator', 'Koordynator')
        cls.reader = create_member('audio-reader', 'Korektor audiobooków')
        cls.member = create_member('audio-editor', 'Redaktor')
        cls.book = Anthology.objects.create(title='Audiobookowa antologia', status=Anthology.Status.READY)
        cls.text = Text.objects.create(title='Głos', anthology=cls.book, length=1234, tags='smoki', genre='fantasy')
        cls.text.authors.add(Author.objects.create(first_name='Prywatne', last_name='Nazwisko', pseudonym='Podpis'))
        cls.url = reverse('core:audiobook_detail', args=[cls.text.pk])
        cls.list_url = reverse('core:audiobooks')
        cls.public = reverse('core:external_audiobooks')

    def token(self, user=None):
        return signing.dumps([(user or self.admin).pk, f'texts.text:{self.text.pk}', version_of(self.text)], salt='cms-edit-version')

    def save_audio(self, user=None, **fields):
        user = user or self.admin
        self.client.force_login(user)
        return self.client.post(self.url, {'status': 'recording', '_edit_version': self.token(user), **fields})

    def test_preview_does_not_write_and_save_keeps_editorial_workflow(self):
        self.client.force_login(self.coordinator)
        count = get_user_model().objects.count()
        self.assertContains(self.client.get(self.url), 'Podpis')
        self.assertFalse(Audiobook.objects.exists())
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())
        response = self.save_audio(self.coordinator, narrator_name='Lektor bez konta', narrator_email='lektor@example.test',
            engineer_name='Dźwiękowiec bez konta', engineer_email='audio@example.test', proofreader=self.reader.pk)
        self.assertEqual(response.status_code, 302)
        audio = Audiobook.objects.get(text=self.text)
        self.assertEqual(audio.proofreader_id, self.reader.pk)
        self.assertIsNone(audio.recording_started_at)
        self.assertEqual(get_user_model().objects.count(), count)
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())
        page = self.client.get(self.list_url)
        for value in ('Lektor bez konta', 'Dźwiękowiec bez konta', 'Trwa nagrywanie', self.url):
            self.assertContains(page, value)
        self.assertNotContains(page, 'Prywatne Nazwisko')

    def test_statuses_dates_links_and_public_eligibility(self):
        for status in Audiobook.Status.values:
            response = self.save_audio(status=status, narrator_name='Lektor', engineer_name='Dźwiękowiec',
                proofreader=self.reader.pk, recording_started_at='2026-10-01', proofreading_started_at='2026-10-02',
                corrections_started_at='2026-10-03', editing_started_at='2026-10-04',
                awaiting_publication_started_at='2026-10-05', premiere_date='2026-10-10',
                youtube_url='https://youtu.be/film', hearthis_url='https://hearthis.at/fantazmaty/opowiadanie/')
            self.assertEqual(response.status_code, 302)
            audio = Audiobook.objects.get(text=self.text)
            self.assertEqual(audio.status, status)
            self.assertEqual(audio.recording_started_at, date(2026, 10, 1))
            self.assertEqual(audio.premiere_date, date(2026, 10, 10))
            public = self.client.get(self.public)
            self.assertEqual(public.context['page_obj'].paginator.count, int(status == 'pending'))
            table = html.fromstring(self.client.get(self.list_url).content)
            self.assertEqual(len(table.xpath('//a[@href="https://youtu.be/film"]')), int(status == 'published'))

    def test_account_role_and_validation_on_web_and_admin_forms(self):
        self.assertEqual(self.save_audio(proofreader=self.member.pk).status_code, 400)
        self.reader.is_active = False
        self.reader.save(update_fields=['is_active'])
        self.assertEqual(self.save_audio(proofreader=self.reader.pk).status_code, 400)
        self.assertEqual(self.save_audio(youtube_url='javascript:alert(1)').status_code, 400)
        self.assertEqual(self.save_audio(hearthis_url='https://hearthis.at.evil.test/file').status_code, 400)
        self.assertEqual(self.save_audio(recording_started_at='not-a-date').status_code, 400)
        self.assertEqual(self.save_audio(narrator_email='name@example.test').status_code, 400)
        self.assertFalse(Audiobook.objects.exists())
        self.reader.is_active = True
        self.reader.save(update_fields=['is_active'])
        self.assertEqual(self.save_audio(proofreader=self.reader.pk).status_code, 302)
        self.reader.is_active = False
        self.reader.save(update_fields=['is_active'])
        self.assertEqual(self.save_audio(proofreader=self.reader.pk, status='published').status_code, 302)

    def test_permissions_and_stale_forms(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        Audiobook.objects.create(text=self.text, narrator_name='Lektor', narrator_email='private-audio@example.test')
        for user in (self.reader, self.member):
            self.client.force_login(user)
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, '>Zapisz</button>')
            self.assertNotContains(response, 'private-audio@example.test')
            self.assertEqual(self.save_audio(user).status_code, 403)
        self.client.force_login(self.admin)
        stale = self.token()
        self.assertEqual(self.save_audio(narrator_name='Pierwszy').status_code, 302)
        self.assertEqual(self.client.post(self.url, {'status': 'pending', '_edit_version': stale}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'status': 'pending'}).status_code, 409)
        self.assertEqual(Audiobook.objects.get(text=self.text).narrator_name, 'Pierwszy')

    def test_disabled_and_withdrawn_preserve_data_but_forbid_web_edits(self):
        self.assertEqual(self.save_audio(narrator_name='Zachowany').status_code, 302)
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=True)
        self.assertEqual(self.client.get(self.list_url).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.save_audio(status='pending').status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=False, for_recording=False)
        self.assertEqual(self.save_audio().status_code, 403)
        Text.objects.filter(pk=self.text.pk).update(for_recording=True)
        WorkflowStage.objects.create(text=self.text, stage_type='withdrawn')
        self.assertEqual(self.save_audio().status_code, 403)
        self.assertEqual(Audiobook.objects.get(text=self.text).narrator_name, 'Zachowany')
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        chapter = Text.objects.create(title='Rozdział 1', anthology=novel, chapter_number=1, length=1)
        self.assertEqual(self.client.get(reverse('core:audiobook_detail', args=[chapter.pk])).status_code, 404)

    def test_filters_sorting_pagination_and_no_per_row_queries(self):
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as single:
            self.client.get(self.list_url)
        Text.objects.bulk_create([Text(title=f'Tekst {i:02}', anthology=self.book, length=1) for i in range(30)])
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.list_url)
        self.assertLessEqual(len(many), len(single) + 2)
        self.assertEqual(response.context['page_obj'].paginator.count, 31)
        page = self.client.get(self.list_url, {'q': 'Tekst', 'status': 'pending', 'sort': '-title', 'page': 2})
        self.assertEqual([row['title'] for row in page.context['texts']], [f'Tekst {i:02}' for i in range(4, -1, -1)])
        for sort in ('author', 'status', 'narrator', 'engineer', 'proofreader', 'recording', 'proofreading',
                     'corrections', 'editing', 'awaiting', 'premiere', 'youtube', 'hearthis', 'authors__email'):
            self.assertEqual(self.client.get(self.list_url, {'sort': sort}).status_code, 200)
        self.assertEqual(self.client.get(self.list_url, {'status': 'bad'}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.list_url, {'anthology': '9' * 100}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.list_url, {'q': 'Podpis'}).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.client.get(self.list_url, {'q': 'Prywatne'}).context['page_obj'].paginator.count, 0)

    def test_translated_author_and_empty_audio_visible(self):
        book = Anthology.objects.create(title='Tłumaczone', is_translated=True)
        text = Text.objects.create(title='Translated', anthology=book, length=1)
        translation, _ = TextTranslation.objects.get_or_create(text=text)
        translation.foreign_authors.add(ForeignAuthor.objects.create(first_name='Foreign', last_name='Author', pseudonym='Foreign Pen'))
        self.client.force_login(self.admin)
        page = self.client.get(self.list_url, {'q': 'Foreign Pen', 'sort': 'author'})
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        self.assertContains(page, 'Foreign Pen')
        self.assertContains(page, 'Do nagrania')
        self.assertFalse(Audiobook.objects.exists())

    def test_admin_uses_same_validation_and_invalidates_web_token(self):
        self.assertEqual(self.save_audio(proofreader=self.reader.pk).status_code, 302)
        audio = Audiobook.objects.get(text=self.text)
        url = reverse('admin:core_audiobook_change', args=[audio.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        doc = html.fromstring(page.content)
        token = doc.xpath('//input[@name="_edit_version"]/@value')[0]
        stale = self.token()
        data = {'status': 'editing', 'engineer_name': 'Montażysta', 'proofreader': self.reader.pk,
            '_edit_version': token, '_save': 'Zapisz'}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(self.url, {'status': 'pending', '_edit_version': stale}).status_code, 409)
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'editing')
        self.assertContains(self.client.get(reverse('admin:index')), 'Audiobooki – przypisania i publikacje')
        from core.admin import AudiobookAdminForm
        form = AudiobookAdminForm({'status': 'editing', 'proofreader': self.member.pk}, instance=audio)
        self.assertFalse(form.is_valid())
        self.assertIn('proofreader', form.errors)
