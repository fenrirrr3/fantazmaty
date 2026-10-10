from datetime import timedelta
from importlib import import_module

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.edit_versions import version_of
from core.models import Audiobook, AudiobookStage
from texts.models import Anthology, Text, TextTranslation, ForeignAuthor
from workflow.models import WorkflowStage
from workflow.tests import create_member


class AudiobookProductionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('audio-manager', 'audio@example.test', 'test')
        cls.coordinator = create_member('audio-coordinator', 'Koordynator')
        cls.reader = create_member('audio-reader', 'Korektor audiobooków')
        cls.other_reader = create_member('other-reader', 'Korektor audiobooków')
        cls.member = create_member('audio-editor', 'Redaktor')
        cls.book = Anthology.objects.create(title='Audiobookowa antologia', status=Anthology.Status.READY)
        cls.text = Text.objects.create(title='Głos', anthology=cls.book, length=1234, tags='smoki', genre='fantasy')
        cls.text.authors.add(Author.objects.create(first_name='Prywatne', last_name='Nazwisko', pseudonym='Podpis'))
        cls.url = reverse('core:audiobook_detail', args=[cls.text.pk])
        cls.list_url = reverse('core:audiobooks')
        cls.queue = reverse('core:audio_proofreading')
        cls.assign_url = reverse('core:assign_audio_proofreader', args=[cls.text.pk])
        cls.public = reverse('core:external_audiobooks')

    def token(self, user=None):
        return signing.dumps([(user or self.admin).pk, f'texts.text:{self.text.pk}', version_of(self.text)], salt='cms-edit-version')

    def post(self, action, user=None, **fields):
        user = user or self.admin
        self.client.force_login(user)
        return self.client.post(self.url, {'action': action, '_edit_version': self.token(user), **fields})

    def start(self, status, user=None, **fields):
        return self.post('start_stage', user, stage_type=status, started_at=fields.get('started_at', str(timezone.localdate())))

    def assign(self, person, user=None):
        user = user or self.admin
        self.client.force_login(user)
        return self.client.post(self.assign_url, {'proofreader': person.pk if person else '', '_edit_version': self.token(user)})

    def test_preview_and_people_form_preserve_editorial_workflow(self):
        self.client.force_login(self.coordinator)
        count = get_user_model().objects.count()
        page = self.client.get(self.url)
        self.assertContains(page, 'Podpis')
        self.assertFalse(Audiobook.objects.exists())
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())
        self.assertNotContains(page, 'name="proofreader"')
        self.assertContains(page, 'audio-detail-columns')
        response = self.post('people', self.coordinator, **{'people-narrator_name': 'Lektor bez konta',
            'people-narrator_email': 'private-audio@example.test', 'people-engineer_name': 'Dźwiękowiec bez konta',
            'status': 'published', 'proofreader': self.reader.pk})
        self.assertEqual(response.status_code, 302)
        audio = Audiobook.objects.get(text=self.text)
        self.assertEqual(audio.status, 'pending')
        self.assertIsNone(audio.proofreader_id)
        self.assertIsNone(audio.recording_started_at)
        self.assertEqual(get_user_model().objects.count(), count)
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(self.url), 'private-audio@example.test')
        self.assertEqual(self.post('people', self.member).status_code, 403)

    def test_ordered_repeated_stages_dates_and_public_eligibility(self):
        states = ['recording', 'proofreading', 'corrections', 'proofreading', 'editing', 'awaiting_publication']
        today = timezone.localdate()
        self.assertEqual(self.post('publication', **{'publication-premiere_date': str(today),
            'publication-youtube_url': 'https://youtu.be/film', 'publication-hearthis_url': 'https://hearthis.at/fantazmaty/story/'}).status_code, 302)
        # "Do nagrania" is the starting state, not a stage.
        self.assertEqual(self.start('pending').status_code, 400)
        for state in states:
            self.assertEqual(self.start(state).status_code, 302)
            audio = Audiobook.objects.get(text=self.text)
            self.assertEqual(audio.status, state)
            stage = audio.active_stage
            self.assertEqual(stage.started_at, today)
            # Everything not yet published is listed publicly (with its status).
            self.assertEqual(self.client.get(self.public).context['page_obj'].paginator.count, 1)
            table = html.fromstring(self.client.get(self.list_url).content)
            self.assertEqual(len(table.xpath('//a[@href="https://youtu.be/film"]')), 0)
            self.assertEqual(self.post('finish_stage', stage_id=stage.pk).status_code, 302)
            stage.refresh_from_db()
            self.assertEqual(stage.ended_at, today)
            self.assertTrue(stage.is_completed)
            self.assertIsNone(Audiobook.objects.get(text=self.text).active_stage_id)
        # Going back to an earlier stage is refused.
        self.assertEqual(self.start('recording').status_code, 400)
        # Publication is final: no active stage, nothing to finish.
        self.assertEqual(self.start('published').status_code, 302)
        audio = Audiobook.objects.get(text=self.text)
        self.assertEqual(audio.status, 'published')
        self.assertIsNone(audio.active_stage_id)
        self.assertEqual(self.client.get(self.public).context['page_obj'].paginator.count, 0)
        table = html.fromstring(self.client.get(self.list_url).content)
        self.assertEqual(len(table.xpath('//a[@href="https://youtu.be/film"]')), 1)
        self.assertEqual(list(self.text.audiobook_stages.values_list('stage_type', flat=True)), [*states, 'published'])
        self.assertEqual(self.text.audiobook_stages.filter(stage_type='proofreading').count(), 2)

    def test_stage_validation_and_stale_or_repeated_posts(self):
        self.assertEqual(self.start('invalid').status_code, 400)
        self.assertEqual(self.start('recording', started_at=str(timezone.localdate() + timedelta(days=1))).status_code, 400)
        self.assertFalse(AudiobookStage.objects.exists())
        self.assertEqual(self.start('recording').status_code, 302)
        audio = Audiobook.objects.get(text=self.text)
        stale = self.token()
        self.assertEqual(self.start('editing').status_code, 400)
        self.assertEqual(self.post('finish_stage', stage_id=999999).status_code, 400)
        self.assertEqual(self.post('finish_stage', stage_id=audio.active_stage_id).status_code, 302)
        self.assertEqual(self.client.post(self.url, {'action': 'start_stage', 'stage_type': 'editing',
            'started_at': str(timezone.localdate()), '_edit_version': stale}).status_code, 409)
        self.assertEqual(self.post('finish_stage', stage_id=audio.active_stage_id).status_code, 400)
        self.assertEqual(self.start('editing', started_at=str(timezone.localdate() - timedelta(days=1))).status_code, 400)
        self.assertEqual(AudiobookStage.objects.count(), 1)
        self.assertEqual(self.client.post(self.url, {'action': 'start_stage'}).status_code, 409)

    def test_proofreading_queue_scope_assignment_and_own_finish(self):
        self.assertEqual(self.start('proofreading').status_code, 302)
        other_book = Anthology.objects.create(title='Antologia innego korektora')
        other = Text.objects.create(title='Cudzy audiobook', anthology=other_book, length=1)
        Audiobook.objects.create(text=other, status='proofreading', proofreader=self.other_reader)
        not_correction = Text.objects.create(title='Trwa montaż', anthology=self.book, length=1)
        Audiobook.objects.create(text=not_correction, status='editing', proofreader=self.reader)
        self.assertEqual(self.assign(self.reader).status_code, 302)
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            page = self.client.get(self.queue)
            self.assertEqual(page.context['page_obj'].paginator.count, 2)
            self.assertContains(page, 'name="proofreader"')
        self.client.force_login(self.reader)
        page = self.client.get(self.queue)
        self.assertEqual([r['pk'] for r in page.context['texts']], [self.text.pk])
        self.assertContains(page, self.url)
        self.assertNotContains(page, 'Cudzy audiobook')
        self.assertNotContains(page, other_book.title)
        self.assertNotContains(page, 'name="proofreader"')
        self.assertEqual(self.assign(self.other_reader, self.reader).status_code, 403)
        stage = Audiobook.objects.get(text=self.text).active_stage
        self.assertEqual(self.post('finish_stage', self.other_reader, stage_id=stage.pk).status_code, 403)
        self.assertEqual(self.post('finish_stage', self.reader, stage_id=stage.pk).status_code, 302)
        self.assertEqual(self.start('editing', self.reader).status_code, 403)
        self.assertEqual(self.start('editing').status_code, 302)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.queue).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.queue, {'hide_completed': '0'}).context['page_obj'].paginator.count, 1)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.queue).status_code, 403)
        doc = html.fromstring(self.client.get(self.list_url).content)
        self.assertFalse(doc.xpath(f'//nav//a[@href="{self.queue}"]|//aside//a[@href="{self.queue}"]'))
        self.client.logout()
        self.assertEqual(self.client.get(self.queue).status_code, 302)

    def test_assignment_validates_roles_inactive_users_and_status(self):
        self.assertEqual(self.start('recording').status_code, 302)
        self.assertEqual(self.assign(self.reader).status_code, 404)
        stage = Audiobook.objects.get(text=self.text).active_stage
        self.post('finish_stage', stage_id=stage.pk)
        self.start('proofreading')
        self.assertEqual(self.assign(self.member).status_code, 400)
        self.reader.is_active = False
        self.reader.save(update_fields=['is_active'])
        self.assertEqual(self.assign(self.reader).status_code, 400)
        self.reader.is_active = True
        self.reader.save(update_fields=['is_active'])
        self.assertEqual(self.assign(self.reader).status_code, 302)
        stale = self.token()
        self.assertEqual(self.assign(self.other_reader).status_code, 302)
        self.assertEqual(self.client.post(self.assign_url, {'proofreader': self.reader.pk, '_edit_version': stale}).status_code, 409)
        self.assertEqual(Audiobook.objects.get(text=self.text).proofreader_id, self.other_reader.pk)
        self.assertEqual(self.post('publication', **{'publication-youtube_url': 'javascript:alert(1)'}).status_code, 400)
        self.assertEqual(self.post('publication', **{'publication-hearthis_url': 'https://hearthis.at.evil.test/file'}).status_code, 400)

    def test_disabled_and_withdrawn_preserve_history_and_forbid_mutations(self):
        self.start('proofreading')
        self.assign(self.reader)
        stage = Audiobook.objects.get(text=self.text).active_stage
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=True)
        self.assertEqual(self.client.get(self.list_url).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.queue).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.client.get(self.queue, {'hide_completed': '0'}).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.post('finish_stage', stage_id=stage.pk).status_code, 403)
        self.assertEqual(self.assign(None).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=False, for_recording=False)
        self.assertEqual(self.post('finish_stage', stage_id=stage.pk).status_code, 403)
        Text.objects.filter(pk=self.text.pk).update(for_recording=True)
        WorkflowStage.objects.create(text=self.text, stage_type='withdrawn')
        self.assertEqual(self.post('finish_stage', stage_id=stage.pk).status_code, 403)
        self.assertEqual(Audiobook.objects.get(text=self.text).active_stage_id, stage.pk)
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        chapter = Text.objects.create(title='Rozdział 1', anthology=novel, chapter_number=1, length=1)
        self.assertEqual(self.client.get(reverse('core:audiobook_detail', args=[chapter.pk])).status_code, 404)

    def test_filters_sorting_pagination_and_query_count(self):
        self.start('proofreading')
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as single:
            self.client.get(self.queue)
        Text.objects.bulk_create([Text(title=f'Tekst {i:02}', anthology=self.book, length=1) for i in range(30)])
        # MySQL nie zwraca kluczy z bulk_create, więc teksty czytamy ponownie.
        texts = Text.objects.filter(anthology=self.book, title__startswith='Tekst ')
        Audiobook.objects.bulk_create([Audiobook(text=t, status='proofreading') for t in texts])
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.queue)
        self.assertLessEqual(len(many), len(single) + 2)
        self.assertEqual(response.context['page_obj'].paginator.count, 31)
        for route in (self.queue, self.list_url):
            page = self.client.get(route, {'q': 'Tekst', 'sort': '-title', 'page': 2})
            self.assertEqual([row['title'] for row in page.context['texts']], [f'Tekst {i:02}' for i in range(4, -1, -1)])
            for sort in ('author', 'status', 'narrator', 'engineer', 'proofreader', 'recording', 'proofreading', 'corrections', 'editing', 'awaiting', 'premiere', 'youtube', 'hearthis', 'authors__email'):
                self.assertEqual(self.client.get(route, {'sort': sort}).status_code, 200)
        self.assertEqual(self.client.get(self.list_url, {'status': 'bad'}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.list_url, {'anthology': '9' * 100}).context['page_obj'].paginator.count, 0)

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

    def test_admin_preserves_status_history_and_invalidates_web_token(self):
        self.start('recording')
        audio = Audiobook.objects.get(text=self.text)
        url = reverse('admin:core_audiobook_change', args=[audio.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="status"')
        token = html.fromstring(page.content).xpath('//input[@name="_edit_version"]/@value')[0]
        stale = self.token()
        data = {'status': 'editing', 'engineer_name': 'Montażysta', 'proofreader': self.reader.pk, '_edit_version': token, '_save': 'Zapisz'}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'recording')
        self.assertEqual(audio.engineer_name, 'Montażysta')
        self.assertEqual(self.client.post(self.url, {'action': 'people', '_edit_version': stale}).status_code, 409)
        self.assertContains(self.client.get(reverse('admin:index')), 'Historia etapów audiobooków')
        history = reverse('admin:core_audiobookstage_change', args=[audio.active_stage_id])
        self.assertEqual(self.client.get(history).status_code, 200)
        self.assertEqual(self.client.post(history, {'is_completed': 'on'}).status_code, 403)

    def test_migration_preserves_known_dates_without_inventing_end_dates(self):
        apps = MigrationExecutor(connection).loader.project_state([('core', '0027_audiobook_stages')]).apps
        old = timezone.localdate() - timedelta(days=10)
        audio = Audiobook.objects.create(text_id=self.text.pk, status='proofreading', recording_started_at=old)
        migration = import_module('core.migrations.0028_audiobook_stage_history')
        migration.preserve_dates(apps, connection.schema_editor())
        migration.preserve_dates(apps, connection.schema_editor())
        audio.refresh_from_db()
        stages = list(self.text.audiobook_stages.all())
        self.assertEqual(len(stages), 2)
        self.assertEqual(stages[0].started_at, old)
        self.assertTrue(stages[0].is_completed)
        self.assertIsNone(stages[0].ended_at)
        self.assertEqual(audio.active_stage_id, stages[1].pk)
        self.assertIsNone(stages[1].started_at)
        self.assertFalse(stages[1].is_completed)
