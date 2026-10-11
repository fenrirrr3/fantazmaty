"""Produkcja audiobooka na stronie audiobooka: stała kolejność etapów, daty, publikacja, blokady i panel admina."""
from datetime import date, timedelta

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.admin import AudiobookAdmin
from core.models import Audiobook, AudiobookStage, WorkflowEvent
from core.selectors.texts import text_list_context
from fantazmaty.testing import form_value
from texts.models import Anthology, Text
from workflow.models import WorkflowStage

from .support import AudiobookProductionTestData


class ProductionWorkflowTests(AudiobookProductionTestData, TestCase):
    def test_preview_and_people_form_preserve_editorial_workflow(self):
        self.client.force_login(self.coordinator)
        count = get_user_model().objects.count()
        page = self.client.get(self.url)
        self.assertContains(page, 'Podpis')
        self.assertFalse(Audiobook.objects.exists())
        self.assertNotContains(page, 'name="proofreader"')
        self.assertContains(page, 'audio-detail-columns')
        response = self.post('people', self.coordinator, **{
            'people-narrator_name': 'Lektor bez konta', 'people-narrator_email': 'private-audio@example.test',
            'people-engineer_name': 'Dźwiękowiec bez konta', 'status': 'published', 'proofreader': self.reader.pk})
        self.assertEqual(response.status_code, 302)
        audio = self.audio()
        self.assertEqual(audio.status, 'pending')
        self.assertIsNone(audio.proofreader_id)
        self.assertFalse(AudiobookStage.objects.exists())
        self.assertEqual(get_user_model().objects.count(), count)
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(self.url), 'private-audio@example.test')
        self.assertEqual(self.post('people', self.member).status_code, 403)

    def test_full_production_in_fixed_order_and_publication(self):
        today = timezone.localdate()
        self.assertEqual(set(Audiobook.Status.labels),
                         {'Do wzięcia', 'Nagrywanie', 'Korekta', 'Nanoszenie poprawek', 'Montaż', 'Opublikowane'})
        self.assertEqual(self.client.get(self.public).context['page_obj'].paginator.count, 1)
        # Do wzięcia → Nagrywanie: rozpoczęcie nagrywania to wzięcie tekstu.
        self.assertEqual(self.start(self.reader).status_code, 403)
        self.assertEqual(self.start().status_code, 302)
        audio = self.audio()
        self.assertEqual((audio.status, audio.status_label, audio.active_stage.started_at),
                         ('recording', 'Nagrywanie', today))
        self.assertEqual(self.finish().status_code, 302)
        # Koniec nagrywania udostępnia korektę; rozpoczyna ją korektor, przejmując ją.
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage.queued_at, audio.active_stage.is_started),
                         ('proofreading', today, False))
        self.assertEqual(self.finish().status_code, 400)  # nierozpoczętego etapu nie można zakończyć
        self.assertEqual(self.claim(self.reader).status_code, 302)
        audio = self.audio()
        self.assertEqual((audio.status, audio.proofreader, audio.active_stage.performer,
                          audio.active_stage.started_at), ('proofreading', self.reader, self.reader, today))
        self.assertEqual(self.finish(self.reader, day=False).status_code, 302)
        # Koniec korekty – gotowe do nanoszenia poprawek, potem montaż.
        for status in ('corrections', 'editing'):
            audio = self.audio()
            self.assertEqual((audio.status, audio.active_stage.stage_type, audio.active_stage.is_started,
                              audio.active_stage.queued_at), (status, status, False, today))
            self.assertEqual(self.finish().status_code, 400)
            self.assertEqual(self.start(self.reader).status_code, 403)
            self.assertEqual(self.start().status_code, 302)
            self.assertEqual(self.audio().status, status)
            self.assertEqual(self.finish().status_code, 302)
        # Koniec montażu – Opublikowane; data premiery i linki uzupełnia się w Publikacji.
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage_id, audio.is_published), ('published', None, True))
        self.assertEqual(self.start().status_code, 400)
        stages = list(AudiobookStage.objects.filter(text=self.text).order_by('position'))
        self.assertEqual([(s.stage_type, s.position) for s in stages],
                         [('recording', 1), ('proofreading', 2), ('corrections', 3), ('editing', 4)])
        self.assertTrue(all(s.is_completed and s.started_at == today and s.ended_at == today for s in stages))
        self.assertTrue(all(event.channel for event in WorkflowEvent.objects.filter(kind='audiobook', text=self.text)))
        self.assertContains(self.client.get(self.url), 'Uzupełnij datę premiery')
        self.assertEqual(self.post('publication', **{'publication-premiere_date': str(today),
                                                     'publication-youtube_url': 'https://youtu.be/film',
                                                     'publication-hearthis_url': ''}).status_code, 302)
        self.assertEqual(self.client.get(self.public).context['page_obj'].paginator.count, 0)
        table = html.fromstring(self.client.get(self.list_url).content)
        self.assertEqual(len(table.xpath('//a[@href="https://youtu.be/film"]')), 1)
        self.assertEqual(self.client.get(self.list_url, {'status': 'published'}).context['page_obj'].paginator.count, 1)
        # Usunięcie linków nie cofa publikacji – wynika ona z zakończonego montażu.
        self.post('publication', **{'publication-premiere_date': '', 'publication-youtube_url': '',
                                    'publication-hearthis_url': ''})
        self.assertEqual(self.audio().status, 'published')

    def test_optional_dates_only_for_coordinator(self):
        # Koordynator może zostawić datę pustą – „data nieznana”.
        self.assertEqual(self.start(day='').status_code, 302)
        stage = self.audio().active_stage
        self.assertEqual((stage.started_at, stage.start_unknown, stage.is_started), (None, True, True))
        self.assertEqual(self.finish(day='').status_code, 302)
        stage.refresh_from_db()
        self.assertEqual((stage.ended_at, stage.is_completed), (None, True))
        # Korekta dostępna od dziś (koniec nagrywania nieznany).
        self.assertEqual(self.audio().active_stage.queued_at, timezone.localdate())
        page = html.fromstring(self.client.get(self.url).content)
        row = page.xpath('//table/tbody/tr[td[1][starts-with(normalize-space(.),"Nagrywanie")]]')[0]
        self.assertEqual(["".join(td.itertext()).strip() for td in row.xpath('./td')][2:], ['?', '?', 'Zakończony'])
        # Korektor nie wybiera daty: zawsze dzisiaj.
        self.claim(self.reader)
        self.assertEqual(self.finish(self.reader, day='').status_code, 302)
        done = AudiobookStage.objects.get(text=self.text, stage_type='proofreading')
        self.assertEqual(done.ended_at, timezone.localdate())

    def test_stage_validation_stale_and_unknown_actions(self):
        self.assertEqual(self.start(day=timezone.localdate() + timedelta(days=1)).status_code, 400)
        self.assertFalse(AudiobookStage.objects.exists())
        self.assertEqual(self.start().status_code, 302)
        self.assertEqual(self.start().status_code, 400)  # już trwa
        stale = self.token()
        self.assertEqual(self.post('finish_stage', stage_id=999999).status_code, 400)
        self.assertEqual(self.finish(day=timezone.localdate() - timedelta(days=1)).status_code, 400)
        self.assertEqual(self.finish().status_code, 302)
        self.assertEqual(self.client.post(self.url, {'action': 'start_stage', '_edit_version': stale}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'action': 'start_stage'}).status_code, 409)
        # Nieznane akcje (np. dawne „queue” i „pause”) są odrzucane.
        for action in ('queue', 'pause'):
            self.assertEqual(self.post(action).status_code, 400)
        self.assertEqual(AudiobookStage.objects.count(), 2)

    def test_disabled_and_withdrawn_preserve_history_and_forbid_mutations(self):
        self.start()
        self.finish()
        self.assign(self.reader)
        self.start()
        stage = self.audio().active_stage
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=True)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.list_url).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.queue_url).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.finish().status_code, 403)
        self.assertEqual(self.assign(None).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=False, for_recording=False)
        self.assertEqual(self.finish().status_code, 403)
        Text.objects.filter(pk=self.text.pk).update(for_recording=True)
        WorkflowStage.objects.create(text=self.text, stage_type='withdrawn')
        self.assertEqual(self.finish().status_code, 403)
        self.assertEqual(self.start().status_code, 403)
        self.assertEqual(self.audio().active_stage_id, stage.pk)
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        chapter = Text.objects.create(title='Rozdział 1', anthology=novel, chapter_number=1, length=1)
        self.assertEqual(self.client.get(reverse('core:audiobook_detail', args=[chapter.pk])).status_code, 404)

    def test_disabled_audiobook_metadata_editable_but_stages_blocked(self):
        audio = Audiobook.objects.create(text=self.text, status='published', narrator_name='Pierwotny lektor')
        self.text.audiobook_blacklisted = True
        self.text.save()
        self.client.force_login(self.admin)
        page = self.client.get(self.url)
        self.assertTrue(page.context['can_edit'])
        self.assertFalse(page.context['can_start'] or page.context['can_finish'])
        self.assertNotContains(page, 'Dodaj etap')
        data = {'publication-premiere_date': '2020-05-04',
                'publication-youtube_url': 'https://www.youtube.com/watch?v=abcdefghijk',
                'publication-hearthis_url': ''}
        self.assertEqual(self.post('publication', **data).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.premiere_date, date(2020, 5, 4))
        self.assertEqual(audio.status, 'published')
        self.assertEqual(self.start().status_code, 403)
        self.assertEqual(self.post('publication', self.member, **data).status_code, 403)

    def test_disabled_text_without_audio_cannot_create_metadata(self):
        self.text.for_recording = False
        self.text.save()
        self.client.force_login(self.admin)
        self.assertFalse(self.client.get(self.url).context['can_edit'])
        self.assertEqual(self.post('people', **{'people-narrator_name': 'Nowy lektor'}).status_code, 403)
        self.assertFalse(Audiobook.objects.exists())

    def test_abandoned_audio_metadata_remains_accessible_without_resuming_production(self):
        audio = Audiobook.objects.create(text=self.text, status='published')
        self.book.status = 'abandoned'
        self.book.save()
        self.client.force_login(self.admin)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context['can_edit'])
        self.assertFalse(page.context['can_start'] or page.context['can_finish'])
        self.assertEqual(self.post('people', **{'people-narrator_name': 'Uzupełniony lektor'}).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.narrator_name, 'Uzupełniony lektor')
        self.assertEqual(audio.status, 'published')

    def test_text_preview_shows_real_audio_status_without_creating_record(self):
        audio = Audiobook.objects.create(text=self.text, status='published')
        self.client.force_login(self.admin)
        url = reverse('core:assigned_text_detail', args=[self.text.pk])
        for value, label in Audiobook.Status.choices:
            audio.status = value
            audio.save(update_fields=['status'])
            section = html.fromstring(self.client.get(url).content).get_element_by_id('text-audiobook')
            self.assertIn(label, section.text_content())
        empty = Text.objects.create(title='Bez produkcji', anthology=self.book, length=1)
        self.assertContains(self.client.get(reverse('core:assigned_text_detail', args=[empty.pk])), 'Do wzięcia')
        self.assertFalse(Audiobook.objects.filter(text=empty).exists())

    def test_audiobook_events_do_not_change_text_status_sorting(self):
        self.start()
        rows = text_list_context(user=self.admin, params={})['texts']
        self.assertIsNone(next(row for row in rows if row['pk'] == self.text.pk)['last_status_change'])

    def test_admin_preserves_status_and_corrects_history_dates(self):
        self.start(day='')
        audio = self.audio()
        request = RequestFactory().get('/')
        request.user = self.admin
        # Korektora zmienia się w panelu tylko podczas korekty.
        self.assertIn('proofreader', AudiobookAdmin(Audiobook, site).get_readonly_fields(request, audio))
        url = reverse('admin:core_audiobook_change', args=[audio.pk])
        self.client.force_login(self.admin)
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="status"')
        stale = self.token()
        data = {'status': 'editing', 'engineer_name': 'Montażysta', 'proofreader': self.reader.pk,
                '_edit_version': form_value(page), '_save': 'Zapisz'}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'recording')
        self.assertEqual(audio.engineer_name, 'Montażysta')
        self.assertEqual(self.client.post(self.url, {'action': 'people', '_edit_version': stale}).status_code, 409)
        self.assertContains(self.client.get(reverse('admin:index')), 'Historia etapów audiobooków')
        history = reverse('admin:core_audiobookstage_change', args=[audio.active_stage_id])
        page = self.client.get(history)
        self.assertEqual(page.status_code, 200)
        fields = {'queued_at': '', 'started_at': '2026-01-05', 'start_unknown': 'on', 'ended_at': '',
                  'performer': '', '_save': 'Zapisz'}
        token = html.fromstring(page.content).xpath('//input[@name="_edit_version"]/@value')
        if token:
            fields['_edit_version'] = token[0]
        # Trwający etap nie dostaje końca z panelu – kończy się go na stronie audiobooka.
        self.assertEqual(self.client.post(history, {**fields, 'ended_at': '2026-01-06'}).status_code, 200)
        self.assertEqual(self.client.post(history, fields).status_code, 302)
        stage = AudiobookStage.objects.get(pk=audio.active_stage_id)
        self.assertEqual((stage.started_at, stage.start_unknown, stage.is_completed), (date(2026, 1, 5), False, False))
        self.assertEqual(self.audio().status, 'recording')
