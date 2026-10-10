import json
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.models import Audiobook, AudiobookStage, PostLayoutAssignment
from core.palettes import label_palette
from core.post_layout import selection_token
from core.selectors.texts import text_list_context
from core.translation_scope import ordinary
from texts.models import Anthology, Text
from workflow.models import WorkflowStage
from workflow.services import claim_ready_for_editing
from workflow.tests import create_member


class UIChangesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = create_member('v68manager', 'Koordynator korekty')
        cls.reader = create_member('v68reader', 'Korektor poskładowy')
        cls.admin = get_user_model().objects.create_superuser('v68admin', 'v68@example.test', 'test')
        cls.book = Anthology.objects.create(title='Porzucona antologia', status='abandoned')
        cls.text = Text.objects.create(title='Porzucony tekst', anthology=cls.book, length=1)
        cls.stage = WorkflowStage.objects.create(text=cls.text, stage_type='ready_for_editing', is_released=True)

    def test_abandoned_scopes_filters_admin_and_direct_claim(self):
        self.assertFalse(ordinary(Anthology.objects).filter(pk=self.book.pk).exists())
        self.assertFalse(ordinary(Text.objects).filter(pk=self.text.pk).exists())
        self.client.force_login(self.admin)
        for name in ('core:anthology_list', 'core:available_texts', 'core:text_list', 'core:workflow_list'):
            page = self.client.get(reverse(name), {'sort': 'anthology', 'hide_ready': '0'})
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, self.book.title)
        page = self.client.get(reverse('admin:texts_anthology_change', args=[self.book.pk]))
        self.assertContains(page, 'Porzucona')
        context = text_list_context(user=self.admin, params={'anthology': str(self.book.pk), 'hide_ready': '0'})
        self.assertFalse(context['filtered_queryset'].exists())
        with self.assertRaises(ValidationError):
            claim_ready_for_editing(self.text, self.manager)
        self.assertTrue(WorkflowStage.objects.filter(pk=self.stage.pk).exists())
        self.book.status = 'in_preparation'
        self.book.save()
        self.assertTrue(ordinary(Text.objects).filter(pk=self.text.pk).exists())
        self.assertContains(self.client.get(reverse('core:text_list')), self.text.title)

    def test_coordinator_delete_and_conditional_bulk_markup(self):
        item = PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.reader,
            page_from=1, page_to=4, created_by=self.manager)
        self.client.force_login(self.manager)
        url = reverse('core:post_layout')
        doc = html.fromstring(self.client.get(url).content)
        self.assertEqual(set(doc.xpath('//*[@data-bulk-action][@hidden]/@data-bulk-action')), {'status', 'assign', 'delete'})
        self.assertTrue(doc.xpath('//script[contains(@src,"post-layout.js")]'))
        data = {'action':'bulk', 'operation':'delete', 'selected':[selection_token(self.manager, item)]}
        self.assertEqual(self.client.post(url, data).status_code, 400)
        self.assertEqual(self.client.post(url, {**data, 'confirm_delete':'1'}).status_code, 302)
        self.assertFalse(PostLayoutAssignment.objects.filter(pk=item.pk).exists())

    def test_new_role_palettes_are_distinct_and_shared(self):
        roles = ['Prawa ręka', 'Ilustrator', 'Grafik', 'Lektor', 'Składacz', 'Dźwiękowiec']
        colors = [label_palette(role) for role in roles]
        self.assertEqual(len(set(colors)), 6)
        self.assertNotIn('', colors)
        self.assertFalse(set(colors) & {label_palette(n) for n in ('Redaktor', 'Korektor', 'Weryfikator', 'Recenzent', 'Koordynator korekty')})
        from people.models import Role
        for name in roles:
            role, _ = Role.objects.get_or_create(name=name)
            self.reader.person_profile.roles.add(role)
        self.client.force_login(self.admin)
        for url in (reverse('core:people_list'), reverse('core:person_detail', args=[self.reader.person_profile.pk])):
            page = self.client.get(url)
            for palette in colors:
                self.assertContains(page, f'data-palette="{palette}"')

    def test_other_department_coordinator_can_edit_and_delete(self):
        coordinator = create_member('v68editor', 'Koordynator redakcji')
        item = PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.reader,
            page_from=1, page_to=4, created_by=self.manager)
        self.client.force_login(coordinator)
        self.assertEqual(self.client.get(reverse('core:post_layout_edit', args=[item.pk])).status_code, 200)
        data = {'action':'bulk', 'operation':'delete', 'selected':[selection_token(coordinator, item)], 'confirm_delete':'1'}
        self.assertEqual(self.client.post(reverse('core:post_layout'), data).status_code, 302)
        self.assertFalse(PostLayoutAssignment.objects.filter(pk=item.pk).exists())

    def test_translation_person_buttons_in_one_action_group(self):
        book = Anthology.objects.create(title='Tłumaczona', is_translated=True)
        text = Text.objects.create(title='Tekst tłumaczony', anthology=book, length=1)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:translation_detail', args=[text.pk]))
        self.assertEqual(page.status_code, 200)
        doc = html.fromstring(page.content)
        group = doc.xpath('//div[@class="translation-person-actions"]')[0]
        self.assertEqual(len(group.xpath('./a[contains(@class,"secondary-button")]')),2)
        self.assertEqual(group[-1].text_content(),'Zapisz osoby')


class PendingAudioImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = create_member('v68audio', 'Korektor audiobooków')
        cls.book = Anthology.objects.create(title='Antologia importu', status='ready')
        cls.text = Text.objects.create(title='Tekst importu', anthology=cls.book, length=1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.row = {'title':self.text.title, 'anthology':self.book.title, 'status':'proofreading',
            'narrator':'Lektor testowy', 'proofreader':str(self.reader.person_profile), 'engineer':'',
            'stages':[{'type':'proofreading', 'started_at':'2026-09-22', 'ended_at':'2026-09-24', 'completed':True, 'active':False}]}

    def run_import(self, rows=None, apply=False):
        data = self.root/'input.json'
        data.write_text(json.dumps({'schema':'fantazmaty-pending-audio-v1', 'records':rows or [self.row]}), encoding='utf-8')
        call_command('import_pending_audiobooks', str(data), apply=apply, report=str(self.root/'report'), stdout=StringIO())
        return json.loads((self.root/'report.json').read_text(encoding='utf-8'))

    def test_preview_apply_repeat_and_proofreading_finished_without_next_stage(self):
        self.run_import()
        self.assertFalse(Audiobook.objects.exists())
        self.assertFalse(AudiobookStage.objects.exists())
        self.run_import(apply=True)
        audio = Audiobook.objects.get()
        self.assertEqual(audio.status, 'proofreading')
        self.assertIsNone(audio.active_stage_id)
        self.assertEqual(AudiobookStage.objects.get().ended_at, date(2026,9,24))
        self.assertTrue(AudiobookStage.objects.get().is_completed)
        self.assertEqual(self.run_import(apply=True)['counts']['unchanged_audiobooks'], 1)
        self.assertEqual(AudiobookStage.objects.count(), 1)

    def test_active_stage_unknown_dates_and_end_only_editing(self):
        self.row['status']='editing'
        self.row['stages'].append({'type':'editing', 'started_at':None, 'ended_at':None, 'completed':False, 'active':True})
        self.run_import(apply=True)
        audio = Audiobook.objects.get()
        self.assertEqual(audio.active_stage.stage_type, 'editing')
        self.assertIsNone(audio.active_stage.started_at)
        audio.active_stage = None
        audio.status = 'awaiting_publication'
        audio.save()
        with self.assertRaises(CommandError):
            self.run_import(apply=True)
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'awaiting_publication')

    def test_conflict_rolls_back_other_rows_and_keeps_blacklist(self):
        with self.assertRaises(CommandError):
            self.run_import([self.row, {**self.row, 'title':'Nieistniejący tekst'}], apply=True)
        self.assertFalse(Audiobook.objects.exists())
        self.assertFalse(AudiobookStage.objects.exists())
        self.text.audiobook_blacklisted=True
        self.text.save()
        self.row['status']='awaiting_publication'
        self.row['stages'].append({'type':'editing', 'started_at':None, 'ended_at':'2026-09-29', 'completed':True, 'active':False})
        result=self.run_import(apply=True)
        self.assertTrue(result['warnings'])
        self.assertIsNone(Audiobook.objects.get().editing_started_at)
        self.assertEqual(AudiobookStage.objects.get(stage_type='editing').ended_at, date(2026,9,29))
        self.text.refresh_from_db()
        self.assertTrue(self.text.audiobook_blacklisted)
