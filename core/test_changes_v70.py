from datetime import timedelta

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.audiobook_services import claim_proofreading, finish_stage
from core.edit_versions import version_of
from core.models import Audiobook, AudiobookStage, AudioContributor, PostLayoutAssignment
from texts.models import Anthology, AnthologyTask, Text
from workflow.models import WorkflowStage
from workflow.tests import create_member


class AudioClaimTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = create_member('v70reader', 'Korektor audiobooków')
        cls.other = create_member('v70other', 'Korektor audiobooków')
        cls.manager = create_member('v70manager', 'Koordynator korekty')
        cls.book = Anthology.objects.create(title='Korekty nagrań', status='ready')
        cls.text = Text.objects.create(title='Wolna korekta', anthology=cls.book, length=1)
        cls.audio = Audiobook.objects.create(text=cls.text, status='proofreading')
        cls.url = reverse('core:claim_audio_proofreading', args=[cls.text.pk])
        cls.queue = reverse('core:audio_proofreading')

    def token(self, user):
        return signing.dumps([user.pk, f'texts.text:{self.text.pk}', version_of(self.text)], salt='cms-edit-version')

    def test_free_work_claimed_once_records_start_and_can_be_finished(self):
        self.client.force_login(self.reader)
        page = self.client.get(self.queue)
        self.assertTrue(page.context['hide_completed'])
        self.assertContains(page, 'Przejmij')
        self.assertEqual([r['pk'] for r in page.context['texts']], [self.text.pk])
        headers = html.fromstring(page.content).xpath('//table/thead/tr/th/text()')
        self.assertNotIn('Lektor', headers)
        self.assertNotIn('Montaż', headers)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        stale = self.token(self.other)
        self.assertEqual(self.client.post(self.url, {'_edit_version': self.token(self.reader)}).status_code, 302)
        self.audio.refresh_from_db()
        self.assertEqual(self.audio.proofreader, self.reader)
        stage = self.audio.active_stage
        self.assertEqual(stage.started_at, timezone.localdate())
        self.assertEqual(stage.performer, self.reader)
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())
        self.assertNotContains(self.client.get(self.queue), 'Przejmij')
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.url, {'_edit_version': stale}).status_code, 409)
        with self.assertRaises(ValidationError):
            claim_proofreading(text_id=self.text.pk, user=self.other)
        self.assertEqual(AudiobookStage.objects.filter(text=self.text).count(), 1)
        finish_stage(text_id=self.text.pk, stage_id=stage.pk, user=self.reader)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.queue).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.queue, {'hide_completed': '0'}).context['page_obj'].paginator.count, 1)
        with self.assertRaises(ValidationError):
            claim_proofreading(text_id=self.text.pk, user=self.reader)

    def test_preserves_existing_start_and_previous_completed_performer(self):
        old = AudiobookStage.objects.create(text=self.text, stage_type='proofreading', performer=self.reader,
            is_completed=True, ended_at=timezone.localdate() - timedelta(days=5))
        active = AudiobookStage.objects.create(text=self.text, stage_type='proofreading',
            started_at=timezone.localdate() - timedelta(days=2))
        self.audio.active_stage = active
        self.audio.save()
        self.client.force_login(self.reader)
        self.assertContains(self.client.get(self.queue), 'Przejmij')
        claim_proofreading(text_id=self.text.pk, user=self.other)
        active.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(active.started_at, timezone.localdate() - timedelta(days=2))
        self.assertEqual(active.performer, self.other)
        self.assertEqual(old.performer, self.reader)

    def test_role_and_production_guards(self):
        with self.assertRaises(PermissionDenied):
            claim_proofreading(text_id=self.text.pk, user=self.manager)
        self.reader.person_profile.is_external = True
        self.reader.person_profile.save()
        with self.assertRaises(PermissionDenied):
            claim_proofreading(text_id=self.text.pk, user=self.reader)
        for field in ('audiobook_blacklisted', 'for_recording'):
            setattr(self.text, field, field == 'audiobook_blacklisted')
            self.text.save()
            with self.assertRaises(PermissionDenied):
                claim_proofreading(text_id=self.text.pk, user=self.other)
            setattr(self.text, field, field != 'audiobook_blacklisted')
            self.text.save()
        self.book.status = 'abandoned'
        self.book.save()
        with self.assertRaises(PermissionDenied):
            claim_proofreading(text_id=self.text.pk, user=self.other)

    def test_legacy_start_is_preserved_when_claim_creates_missing_stage(self):
        self.audio.proofreader = self.reader
        self.audio.proofreading_started_at = timezone.localdate() - timedelta(days=3)
        self.audio.save()
        stage = claim_proofreading(text_id=self.text.pk, user=self.reader)
        self.assertEqual(stage.started_at, self.audio.proofreading_started_at)
        self.assertIsNone(stage.ended_at)

    def test_detail_contact_buttons_and_proofreader_link(self):
        narrator = AudioContributor.objects.create(name='Lektor Testowy')
        engineer = AudioContributor.objects.create(name='Montażysta Testowy')
        self.audio.narrator_contact = narrator
        self.audio.engineer_contact = engineer
        self.audio.narrator_name = narrator.name
        self.audio.engineer_name = engineer.name
        self.audio.proofreader = self.reader
        self.audio.save()
        self.client.force_login(self.manager)
        page = self.client.get(reverse('core:audiobook_detail', args=[self.text.pk]))
        doc = html.fromstring(page.content)
        for role, person in [('narrator', narrator), ('engineer', engineer)]:
            container = doc.xpath(f'//div[contains(@class,"audio-name-profile")][.//input[@name="people-{role}_name"]]')[0]
            self.assertEqual(container.xpath('.//a/@href'), [reverse('core:audio_contributor', args=[person.pk])])
        self.assertContains(page, reverse('core:person_detail', args=[self.reader.person_profile.pk]))
        self.assertContains(page, 'E-mail dźwiękowca')
        self.assertNotContains(page, 'Przypisz w Korekcie audiobooków')
        self.assertNotContains(page, '<legend>Dźwiękowiec</legend>')


class AdminAnthologyDeleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser('v70admin', 'v70@example.test', 'test')

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
        token = html.fromstring(page.content).xpath('//input[@name="_edit_version"]/@value')[0]
        self.assertEqual(self.client.post(url, {'post': 'yes', '_edit_version': token}).status_code, 302)
        self.assertFalse(Anthology.objects.filter(pk=book.pk).exists())
        self.assertFalse(AnthologyTask.objects.filter(pk__in=tasks).exists())

    def test_assigned_or_commissioned_task_protects_parent_and_bulk_is_atomic(self):
        model_admin = admin.site.get_model_admin(Anthology)
        person = create_member('v70task', 'Składacz').person_profile
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


class PostLayoutUITests(TestCase):
    def test_actions_are_grouped_and_bulk_is_coordinator_only(self):
        manager = create_member('v70layout-manager', 'Koordynator korekty')
        reader = create_member('v70layout-reader', 'Korektor poskładowy')
        book = Anthology.objects.create(title='Poskładowa')
        item = PostLayoutAssignment.objects.create(anthology=book, proofreader=reader,
            page_from=1, page_to=5, created_by=manager)
        self.client.force_login(manager)
        page = self.client.get(reverse('core:post_layout'))
        self.assertContains(page, 'Operacje zbiorcze')
        doc = html.fromstring(page.content)
        actions = doc.xpath('//*[contains(@class,"post-layout-row-actions")]')[0]
        self.assertEqual(actions.xpath('.//button/text()'), ['Zapisz'])
        self.assertEqual(actions.xpath('./a/@href'), [reverse('core:post_layout_edit', args=[item.pk])])
        self.client.force_login(reader)
        self.assertNotContains(self.client.get(reverse('core:post_layout')), 'Operacje zbiorcze')
        self.assertEqual(self.client.post(reverse('core:post_layout'), {'action': 'bulk', 'operation': 'delete'}).status_code, 403)
