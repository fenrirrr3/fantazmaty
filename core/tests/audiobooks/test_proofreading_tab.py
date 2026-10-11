"""Zakładka Korekta audiobooków: zakres widoczności, przejmowanie i przypisywanie korekty, kończenie etapu i historia korekt."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.audiobook_services import claim_proofreading, finish_stage, refresh_status
from core.models import Audiobook, AudiobookStage
from core.selectors.people import profile_assignments
from fantazmaty.testing import create_member, create_superuser, edit_token
from texts.models import Anthology, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage

from .support import AudiobookProductionTestData


class ProofreadingScopeTests(AudiobookProductionTestData, TestCase):
    def test_proofreading_tab_scope_assignment_and_own_finish(self):
        self.start()
        self.finish()
        other_book = Anthology.objects.create(title='Antologia innego korektora')
        other = Text.objects.create(title='Cudzy audiobook', anthology=other_book, length=1)
        mine = Audiobook.objects.create(text=other, proofreader=self.other_reader)
        busy = AudiobookStage.objects.create(text=other, stage_type='proofreading', position=1,
                                             started_at=timezone.localdate(), performer=self.other_reader)
        mine.active_stage, mine.status = busy, 'proofreading'
        mine.save()
        not_correction = Text.objects.create(title='Trwa montaż', anthology=self.book, length=1)
        Audiobook.objects.create(text=not_correction, status='editing', proofreader=self.reader)
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            page = self.client.get(self.queue_url)
            self.assertEqual(page.context['page_obj'].paginator.count, 2)
            self.assertContains(page, 'name="proofreader"')
        # Wolna korekta jest widoczna dla każdego korektora, cudza w toku – nie.
        self.client.force_login(self.reader)
        page = self.client.get(self.queue_url)
        self.assertEqual([r['pk'] for r in page.context['texts']], [self.text.pk])
        self.assertContains(page, 'Przejmij')
        self.assertNotContains(page, 'Cudzy audiobook')
        self.assertNotContains(page, 'name="proofreader"')
        # Koordynator przypisuje oczekującą korektę – korektor rozpoczyna ją sam.
        self.assertEqual(self.assign(self.reader).status_code, 302)
        self.assertEqual(self.assign(self.other_reader, self.reader).status_code, 403)
        self.assertEqual(self.claim(self.other_reader).status_code, 302)  # komunikat: niedostępna
        self.assertEqual(self.audio().proofreader, self.reader)
        self.assertFalse(self.audio().active_stage.is_started)
        self.assertEqual(self.start(self.reader, day=False).status_code, 302)
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage.started_at), ('proofreading', timezone.localdate()))
        self.assertEqual(self.finish(self.other_reader, day=False).status_code, 403)
        self.assertEqual(self.finish(self.reader, day=False).status_code, 302)
        self.assertEqual(self.start(self.reader, day=False).status_code, 403)  # poprawki – koordynator
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.queue_url).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.queue_url, {'hide_completed': '0'}).context['page_obj'].paginator.count, 1)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.queue_url).status_code, 403)
        doc = html.fromstring(self.client.get(self.list_url).content)
        self.assertFalse(doc.xpath(f'//nav//a[@href="{self.queue_url}"]|//aside//a[@href="{self.queue_url}"]'))
        self.client.logout()
        self.assertEqual(self.client.get(self.queue_url).status_code, 302)

    def test_assignment_validates_roles_inactive_users_status_and_publication_links(self):
        self.start()
        self.assertEqual(self.assign(self.reader).status_code, 404)
        self.finish()
        self.assertEqual(self.assign(self.member).status_code, 400)
        self.reader.is_active = False
        self.reader.save(update_fields=['is_active'])
        self.assertEqual(self.assign(self.reader).status_code, 400)
        self.reader.is_active = True
        self.reader.save(update_fields=['is_active'])
        self.assertEqual(self.assign(self.reader).status_code, 302)
        stale = self.token()
        self.assertEqual(self.assign(self.other_reader).status_code, 302)
        self.assertEqual(self.client.post(self.assign_url, {'proofreader': self.reader.pk,
                                                            '_edit_version': stale}).status_code, 409)
        self.assertEqual(self.audio().proofreader_id, self.other_reader.pk)
        # Zmiana korektora trwającej korekty przenosi też wykonawcę etapu.
        self.start()
        self.assertEqual(self.assign(self.reader).status_code, 302)
        self.assertEqual(self.audio().active_stage.performer, self.reader)
        self.assertEqual(self.post('publication', **{'publication-youtube_url': 'javascript:alert(1)'}).status_code, 400)
        self.assertEqual(self.post('publication', **{
            'publication-hearthis_url': 'https://hearthis.at.evil.test/file'}).status_code, 400)


class ClaimProofreadingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = create_member('claim-reader', 'Korektor audiobooków')
        cls.other = create_member('claim-other', 'Korektor audiobooków')
        cls.manager = create_member('claim-manager', 'Koordynator korekty')
        cls.book = Anthology.objects.create(title='Korekty nagrań', status='ready')
        cls.text = Text.objects.create(title='Wolna korekta', anthology=cls.book, length=1)
        cls.audio = Audiobook.objects.create(text=cls.text)
        # Nagranie zakończone – korekta czeka na przejęcie.
        AudiobookStage.objects.create(text=cls.text, stage_type='recording', position=1, is_completed=True,
                                      start_unknown=True)
        AudiobookStage.objects.create(text=cls.text, stage_type='proofreading', position=2)
        refresh_status(cls.audio)
        cls.url = reverse('core:claim_audio_proofreading', args=[cls.text.pk])
        cls.queue = reverse('core:audio_proofreading')

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
        stale = edit_token(self.other, self.text)
        self.assertEqual(self.client.post(self.url, {'_edit_version': edit_token(self.reader, self.text)}).status_code, 302)
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
        self.assertEqual(AudiobookStage.objects.filter(text=self.text).count(), 2)
        finish_stage(text_id=self.text.pk, stage_id=stage.pk, user=self.reader)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.queue).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.queue, {'hide_completed': '0'}).context['page_obj'].paginator.count, 1)
        with self.assertRaises(ValidationError):
            claim_proofreading(text_id=self.text.pk, user=self.reader)

    def test_repeated_proofreading_keeps_previous_performer(self):
        AudiobookStage.objects.filter(text=self.text, stage_type='proofreading').update(
            performer=self.reader, is_completed=True, ended_at=timezone.localdate() - timedelta(days=5))
        AudiobookStage.objects.create(text=self.text, stage_type='corrections', position=3, is_completed=True,
                                      start_unknown=True)
        again = AudiobookStage.objects.create(text=self.text, stage_type='proofreading', position=4)
        refresh_status(self.audio)
        self.assertEqual(self.audio.status, 'proofreading')
        self.client.force_login(self.reader)
        self.assertContains(self.client.get(self.queue), 'Przejmij')
        claim_proofreading(text_id=self.text.pk, user=self.other)
        again.refresh_from_db()
        old = AudiobookStage.objects.get(text=self.text, position=2)
        self.assertEqual((again.started_at, again.performer), (timezone.localdate(), self.other))
        self.assertEqual(old.performer, self.reader)

    def test_started_proofreading_cannot_be_claimed(self):
        AudiobookStage.objects.filter(text=self.text, position=2).update(
            started_at=timezone.localdate() - timedelta(days=2))
        refresh_status(self.audio)
        self.assertEqual(self.audio.status, 'proofreading')
        with self.assertRaises(ValidationError):
            claim_proofreading(text_id=self.text.pk, user=self.other)

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

    def test_claim_creates_missing_stage_for_legacy_data(self):
        Audiobook.objects.filter(pk=self.audio.pk).update(active_stage=None)
        AudiobookStage.objects.filter(text=self.text, position=2).delete()
        self.audio.refresh_from_db()
        refresh_status(self.audio)
        self.assertEqual((self.audio.status, self.audio.active_stage_id), ('proofreading', None))
        stage = claim_proofreading(text_id=self.text.pk, user=self.reader)
        self.assertEqual((stage.stage_type, stage.position, stage.started_at),
                         ('proofreading', 2, timezone.localdate()))
        self.assertIsNone(stage.ended_at)
        self.audio.refresh_from_db()
        self.assertEqual((self.audio.status, self.audio.active_stage_id), ('proofreading', stage.pk))


class FinishFromTableTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = create_member('finish-reader', 'Korektor audiobooków')
        cls.other = create_member('finish-other', 'Korektor audiobooków')
        cls.manager = create_member('finish-manager', 'Koordynator korekty')
        cls.book = Anthology.objects.create(title='Książka testowa', status='ready')
        cls.text = Text.objects.create(title='Korekta do oddania', anthology=cls.book, length=1)
        cls.stage = AudiobookStage.objects.create(text=cls.text, stage_type='proofreading', performer=cls.reader,
                                                  started_at=timezone.localdate() - timedelta(days=3))
        cls.audio = Audiobook.objects.create(text=cls.text, status='proofreading', proofreader=cls.reader,
                                             active_stage=cls.stage)
        cls.queue = reverse('core:audio_proofreading')
        cls.detail = reverse('core:audiobook_detail', args=[cls.text.pk])

    def payload(self, user):
        return {'action': 'finish_stage', 'stage_id': self.stage.pk, 'return_to': 'audio_proofreading',
                '_edit_version': edit_token(user, self.text)}

    def test_finish_from_rendered_table_form(self):
        self.client.force_login(self.reader)
        page = self.client.get(self.queue)
        form = html.fromstring(page.content).xpath('//form[input[@name="action" and @value="finish_stage"]]')[0]
        self.assertEqual(form.xpath('.//button/text()'), ['Zakończ'])
        payload = {i.get('name'): i.get('value') for i in form.xpath('.//input[@name]')}
        self.assertRedirects(self.client.post(form.get('action'), payload), self.queue)
        self.stage.refresh_from_db()
        self.audio.refresh_from_db()
        self.assertEqual(self.stage.ended_at, timezone.localdate())
        self.assertTrue(self.stage.is_completed)
        self.assertEqual(self.stage.performer, self.reader)
        # Po korekcie tekst jest gotowy na nanoszenie poprawek (etap czeka na rozpoczęcie).
        self.assertEqual((self.audio.status, self.audio.active_stage.stage_type), ('corrections', 'corrections'))
        self.assertFalse(self.audio.active_stage.is_started)
        # Po zakończeniu korektor nie ma już uprawnień do etapu (a token jest nieaktualny).
        self.assertIn(self.client.post(self.detail, payload).status_code, (403, 409))
        self.assertNotContains(self.client.get(self.queue, {'hide_completed': '0'}),
                               'name="action" value="finish_stage"')

    def test_coordinator_can_finish_from_table(self):
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(self.queue), 'name="action" value="finish_stage"')
        self.assertRedirects(self.client.post(self.detail, self.payload(self.manager)), self.queue)

    def test_other_reader_cannot_finish_and_disabled_work_is_protected(self):
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(self.queue), 'Korekta do oddania')
        self.assertEqual(self.client.post(self.detail, self.payload(self.other)).status_code, 403)
        self.text.audiobook_blacklisted = True
        self.text.save()
        self.client.force_login(self.reader)
        self.assertNotContains(self.client.get(self.queue), 'name="action" value="finish_stage"')
        self.assertEqual(self.client.post(self.detail, self.payload(self.reader)).status_code, 403)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_reassignment_rejects_previous_reader(self):
        self.client.force_login(self.reader)
        payload = self.payload(self.reader)
        self.audio.proofreader = self.other
        self.audio.save()
        self.assertEqual(self.client.post(self.detail, payload).status_code, 403)
        self.stage.refresh_from_db()
        self.assertFalse(self.stage.is_completed)

    def test_detail_label_and_unknown_completion_date(self):
        historical = AudiobookStage.objects.create(text=self.text, stage_type='recording', is_completed=True,
                                                   position=0)
        self.client.force_login(self.reader)
        doc = html.fromstring(self.client.get(self.detail).content)
        self.assertEqual(doc.xpath('//button[contains(@class,"audio-finish-button")]/text()'), ['Zakończ etap'])
        row_xpath = '//table/tbody/tr[td[1][starts-with(normalize-space(.),"Nagrywanie")]]'
        row = doc.xpath(row_xpath)[0]
        cells = ["".join(td.itertext()).strip() for td in row.xpath('./td')]
        self.assertEqual(cells[2:], ['?', '?', 'Zakończony'])
        historical.ended_at = timezone.localdate()
        historical.save()
        row = html.fromstring(self.client.get(self.detail).content).xpath(row_xpath)[0]
        self.assertEqual("".join(row.xpath('./td')[3].itertext()).strip(),
                         timezone.localdate().strftime('%d.%m.%Y'))


class CorrectionHistoryTests(TestCase):
    """Opublikowany audiobook z dwiema zakończonymi korektami różnych osób."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('hist-admin')
        cls.old = create_member('hist-old', 'Korektor audiobooków')
        cls.new = create_member('hist-new', 'Korektor audiobooków')
        cls.coordinator = create_member('hist-coord', 'Koordynator korekty')
        cls.book = Anthology.objects.create(title='Dawne nagrania', status='ready')
        cls.text = Text.objects.create(title='Nagranie z dwiema korektami', anthology=cls.book, length=1)
        cls.audio = Audiobook.objects.create(text=cls.text, status='published', proofreader=cls.new)
        cls.day = timezone.localdate() - timedelta(days=10)
        cls.first = AudiobookStage.objects.create(text=cls.text, stage_type='proofreading', performer=cls.old,
                                                  started_at=cls.day, ended_at=cls.day + timedelta(days=2),
                                                  is_completed=True)
        cls.second = AudiobookStage.objects.create(text=cls.text, stage_type='proofreading', performer=cls.new,
                                                   started_at=cls.day + timedelta(days=3), is_completed=True)
        cls.queue = reverse('core:audio_proofreading')

    def test_history_visibility_filtering_and_no_duplicate_story_rows(self):
        for user, expected in [(self.old, [self.first.pk]), (self.new, [self.second.pk]),
                               (self.coordinator, [self.second.pk, self.first.pk]),
                               (self.admin, [self.second.pk, self.first.pk])]:
            self.client.force_login(user)
            page = self.client.get(self.queue, {'hide_completed': '0'})
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.context['page_obj'].paginator.count, 1)
            row = list(page.context['texts'])[0]
            self.assertEqual([s['pk'] for s in row['corrections']], expected)
            self.assertFalse(row['allow_assignment'])
            self.assertNotContains(page, 'name="proofreader"')
            self.assertContains(page, reverse('core:audiobook_detail', args=[self.text.pk]))
            self.assertEqual(self.client.get(self.queue, {'status': 'editing'}).context['page_obj'].paginator.count, 0)
            filtered = self.client.get(self.queue, {'status': 'published', 'q': self.text.title, 'hide_completed': '0'})
            self.assertEqual(filtered.context['page_obj'].paginator.count, 1)
        self.client.force_login(create_member('hist-outsider', 'Korektor audiobooków'))
        self.assertEqual(self.client.get(self.queue, {'hide_completed': '0'}).context['page_obj'].paginator.count, 0)

    def test_blacklisted_withdrawn_and_disabled_remain_read_only_in_history(self):
        self.text.for_recording = False
        self.text.audiobook_blacklisted = True
        self.text.save()
        WorkflowStage.objects.create(text=self.text, stage_type='withdrawn')
        self.client.force_login(self.admin)
        page = self.client.get(self.queue, {'hide_completed': '0'})
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        self.assertContains(page, 'Wyłączony z produkcji')
        self.assertFalse(list(page.context['texts'])[0]['allow_assignment'])
        self.assertEqual(self.client.get(reverse('core:external_audiobooks')).context['page_obj'].paginator.count, 0)

    def test_profile_keeps_actual_performer_and_uses_audio_links(self):
        person = self.old.person_profile
        person.is_active = False
        person.save()
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:person_detail', args=[person.pk]))
        self.assertEqual(page.status_code, 200)
        rows = page.context['assignments']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'Audiobook')
        self.assertEqual(rows[0]['latest_stage']['pk'], self.first.pk)
        self.assertTrue(rows[0]['has_completed_work'])
        self.assertEqual(rows[0]['latest_stage']['ended_at'], self.first.ended_at)
        self.assertIsNone(rows[0]['assigned_at'])
        table = html.fromstring(page.content).get_element_by_id('person-assignments-table')
        self.assertIn('Rodzaj', table.xpath('.//th/text()'))
        self.assertNotIn('Etap', table.xpath('.//th/text()'))
        self.assertEqual(set(table.xpath('.//tbody//a[contains(@href,"audiobooki/")]/@href')),
                         {reverse('core:audiobook_detail', args=[self.text.pk])})

    def test_text_assignments_remain_and_missing_history_is_not_invented(self):
        WorkflowRoleAssignment.objects.create(text=self.text, role='editor', assigned_to=self.new)
        rows, _ = profile_assignments(self.new.person_profile, include_authors=True)
        self.assertEqual({row['kind'] for row in rows}, {'Tekst', 'Audiobook'})
        audio = next(row for row in rows if row['kind'] == 'Audiobook')
        self.assertIsNone(audio['latest_stage']['ended_at'])
        unrelated = Text.objects.create(title='Samo przypisanie przed korektą', anthology=self.book, length=1)
        Audiobook.objects.create(text=unrelated, status='recording', proofreader=self.old)
        rows, _ = profile_assignments(self.old.person_profile, include_authors=True)
        self.assertEqual([row['text']['pk'] for row in rows], [self.text.pk])

    def test_current_reassignment_does_not_credit_previous_person(self):
        active = AudiobookStage.objects.create(text=self.text, stage_type='proofreading', performer=self.old,
                                               started_at=timezone.localdate())
        self.audio.status = 'proofreading'
        self.audio.active_stage = active
        self.audio.save()
        for user, expected in [(self.old, {self.first.pk}), (self.new, {self.second.pk, active.pk})]:
            rows, _ = profile_assignments(user.person_profile, include_authors=True)
            self.assertEqual({r['latest_stage']['pk'] for r in rows}, expected)
        self.client.force_login(self.admin)
        page = self.client.get(self.queue, {'hide_completed': '0'})
        self.assertTrue(list(page.context['texts'])[0]['allow_assignment'])

    def test_completed_correction_cannot_be_reassigned_with_forged_post(self):
        self.audio.status = 'proofreading'
        self.audio.save()
        self.client.force_login(self.admin)
        response = self.client.post(reverse('core:assign_audio_proofreader', args=[self.text.pk]),
                                    {'proofreader': self.old.pk, '_edit_version': edit_token(self.admin, self.text)})
        self.assertEqual(response.status_code, 403)
        self.audio.refresh_from_db()
        self.assertEqual(self.audio.proofreader_id, self.new.pk)
        self.second.refresh_from_db()
        self.assertEqual(self.second.performer_id, self.new.pk)


class CorrectionHistoryRenderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('history-render-admin', '', 'test')
        cls.book = Anthology.objects.create(title='Antologia historii korekt')
        cls.text = Text.objects.create(title='Testowe opowiadanie', anthology=cls.book, length=100)
        cls.queue = reverse('core:audio_proofreading')

    def history(self, response):
        return html.fromstring(response.content).xpath('//ul[contains(@class,"audio-correction-history")]')[0]

    def test_unassigned_correction_shows_only_waiting_badge(self):
        audio = Audiobook.objects.create(text=self.text)
        AudiobookStage.objects.create(text=self.text, stage_type='proofreading', position=1, queued_at=date(2026, 8, 1))
        refresh_status(audio)
        self.client.force_login(self.admin)
        page = self.client.get(self.queue, {'hide_completed': '0'})
        self.assertContains(page, 'Do przejęcia')
        self.assertNotContains(page, 'Nie zapisano wykonawcy')
        self.assertEqual(' '.join(self.history(page).text_content().split()), 'Czeka na przejęcie')

    def test_active_correction_history_has_dates_but_no_duplicate_performer(self):
        # Tekst jest jednocześnie w toku redakcji (etap z przypisaną osobą).
        editor = create_member('history-editor', 'Redaktor')
        assignment = WorkflowRoleAssignment.objects.create(text=self.text, role='editor', assigned_to=editor)
        WorkflowStage.objects.create(text=self.text, stage_type='editing', assignment=assignment,
                                     started_at=timezone.localdate())
        person = create_member('Korektorka', 'Korektor audiobooków')
        stage = AudiobookStage.objects.create(text=self.text, stage_type='proofreading', performer=person,
                                              started_at=timezone.localdate())
        Audiobook.objects.create(text=self.text, status='proofreading', proofreader=person, active_stage=stage)
        self.client.force_login(self.admin)
        response = self.client.get(self.queue)
        history = self.history(response)
        self.assertNotIn('Korektorka', history.text_content())
        self.assertFalse(history.xpath('.//a'))
        self.assertIn('Od:', history.text_content())
        self.assertContains(response, str(person.person_profile))

    def test_hide_completed_respects_performer_and_repeated_stage(self):
        old = create_member('repeat-old', 'Korektor audiobooków')
        new = create_member('repeat-new', 'Korektor audiobooków')
        audio = Audiobook.objects.create(text=self.text, status='proofreading', proofreader=new)
        AudiobookStage.objects.create(text=self.text, stage_type='proofreading', performer=old, is_completed=True,
                                      started_at=date(2026, 1, 1), ended_at=date(2026, 1, 2))
        stage = AudiobookStage.objects.create(text=self.text, stage_type='proofreading', performer=new,
                                              started_at=date(2026, 1, 3))
        audio.active_stage = stage
        audio.save()
        for user, count in ((old, 0), (new, 1), (self.admin, 1)):
            self.client.force_login(user)
            page = self.client.get(self.queue, {'hide_completed': '0'})
            self.assertFalse(page.context['hide_completed'])
            self.assertEqual(page.context['page_obj'].paginator.count, 1)
            self.assertEqual(self.client.get(self.queue, {'hide_completed': '1'}).context['page_obj'].paginator.count,
                             count)
        stage.is_completed = True
        stage.ended_at = date(2026, 1, 4)
        stage.save()
        audio.active_stage = None
        audio.status = 'published'
        audio.save()
        self.assertEqual(self.client.get(self.queue, {'hide_completed': '1'}).context['page_obj'].paginator.count, 0)
        for user in (old, new):
            profile = reverse('core:person_detail', args=[user.person_profile.pk])
            self.assertEqual(len(self.client.get(profile).context['assignments']), 1)
            self.assertEqual(len(self.client.get(profile, {'hide_completed': '1'}).context['assignments']), 0)
