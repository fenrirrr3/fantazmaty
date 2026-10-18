"""Wpis audiobooka prowadzony przez koordynatora: osoby ze spisu, ręczny status z historią etapów i datami,
korekta jako jedyny obieg pracy, publikacja, blokady, panel admina i walidacja modelu."""
from datetime import date, timedelta

import json
from unittest.mock import patch

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.admin import AudiobookAdmin
from core.models import AudioContributor, Audiobook, AudiobookStage, DiscordNotification, NotificationRule, WorkflowEvent
from core.selectors.texts import text_list_context
from fantazmaty.testing import form_value
from texts.models import Anthology, Text
from workflow.models import WorkflowStage

from .support import AudiobookProductionTestData


def stage_rows(text):
    return [(s.stage_type, s.started_at, s.start_unknown, s.ended_at, s.is_completed)
            for s in AudiobookStage.objects.filter(text=text).order_by('position')]


class ProductionWorkflowTests(AudiobookProductionTestData, TestCase):
    def test_people_come_from_the_directory_without_accounts(self):
        engineer = AudioContributor.objects.create(name='Montażysta Spisu', email='mont@example.test')
        retired = AudioContributor.objects.create(name='Dawny Lektor', is_active=False)
        self.client.force_login(self.coordinator)
        accounts = get_user_model().objects.count()
        page = self.client.get(self.url)
        self.assertContains(page, 'Podpis')
        self.assertFalse(Audiobook.objects.exists())
        self.assertNotIn(retired, page.context['people_form'].fields['narrator_contact'].queryset)
        rejected = {
            'duplicate of a directory entry': ({'people-new_narrator_name': 'montażysta  spisu'}, 'new_narrator_name'),
            'email without a name': ({'people-new_engineer_email': 'x@example.test'}, 'new_engineer_name'),
            'both a choice and a new person': ({'people-engineer_contact': engineer.pk,
                                                'people-new_engineer_name': 'Ktoś Nowy'}, 'new_engineer_name'),
        }
        for case, (fields, field) in rejected.items():
            with self.subTest(case):
                response = self.post('people', self.coordinator, **fields)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.context['people_form'].errors)
        response = self.post('people', self.coordinator, **{
            'people-new_narrator_name': ' Lektor   bez konta ', 'people-new_narrator_email': 'Private-Audio@example.test',
            'people-engineer_contact': engineer.pk, 'status': 'published', 'proofreader': self.reader.pk})
        self.assertEqual(response.status_code, 302)
        audio = self.audio()
        self.assertEqual((audio.narrator_name, audio.narrator_email, audio.narrator_contact.is_active,
                          audio.engineer_name, audio.status, audio.proofreader_id),
                         ('Lektor bez konta', 'private-audio@example.test', True, 'Montażysta Spisu', 'pending', None))
        self.assertEqual(get_user_model().objects.count(), accounts)
        self.assertFalse(AudiobookStage.objects.exists() or WorkflowStage.objects.filter(text=self.text).exists())
        # Nieaktywny wpis zostaje przy dotychczasowym audiobooku.
        Audiobook.objects.filter(pk=audio.pk).update(narrator_contact=retired)
        self.assertIn(retired, self.client.get(self.url).context['people_form'].fields['narrator_contact'].queryset)
        # Lektor z kontem widzi wpis bez adresów e-mail i niczego nie zmienia; redaktor nie ma dostępu.
        self.client.force_login(self.narrator)
        page = self.client.get(self.url)
        self.assertNotContains(page, 'mont@example.test')
        self.assertFalse(page.context['can_edit'])
        for action in ('people', 'status', 'stages', 'notes', 'publication'):
            with self.subTest(action=action):
                self.assertEqual(self.post(action, self.narrator).status_code, 403)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_status_changes_close_and_open_stages_and_proofreading_is_the_only_workflow(self):
        today = timezone.localdate()
        d1, d2 = today - timedelta(days=9), today - timedelta(days=5)
        self.assertEqual(Audiobook.Status.labels,
                         ['Nieprzypisane', 'Nagrywanie', 'Korekta', 'Nanoszenie poprawek', 'Montaż', 'Opublikowane'])
        self.assertEqual(self.client.get(self.public).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.set_status('recording', self.reader).status_code, 403)
        self.assertEqual(self.set_status('recording', day=d1).status_code, 302)
        self.assertEqual(self.set_status('recording').status_code, 400)  # bez zmiany
        self.assertEqual(self.set_status('proofreading', day=d2).status_code, 302)
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage.stage_type, audio.active_stage.is_started,
                          audio.active_stage.queued_at), ('proofreading', 'proofreading', False, d2))
        # Korekta: korektor przejmuje (dzisiaj) i kończy; status sam przechodzi na „Nanoszenie poprawek”.
        self.assertEqual(self.finish().status_code, 400)
        self.assertEqual(self.claim(self.reader).status_code, 302)
        self.assertEqual(self.finish(self.reader, day=today - timedelta(days=1)).status_code, 302)
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage.stage_type, audio.active_stage.is_started),
                         ('corrections', 'corrections', False))
        self.assertEqual(self.start().status_code, 400)  # rozpocząć można tylko korektę
        # Kolejne statusy ustawia koordynator; pusta data – nieznana.
        self.assertEqual(self.set_status('editing', day='').status_code, 302)
        self.assertEqual(self.set_status('published').status_code, 302)
        self.assertEqual(stage_rows(self.text), [
            ('recording', d1, False, d2, True),
            ('proofreading', today, False, today, True),
            ('corrections', None, True, None, True),
            ('editing', None, True, today, True)])
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage_id, audio.is_published), ('published', None, True))
        self.assertEqual(AudiobookStage.objects.get(stage_type='proofreading').performer, self.reader)
        # Publikacja to dane wpisu – nie zmienia statusu.
        self.assertContains(self.client.get(self.url), 'Uzupełnij datę premiery')
        self.assertEqual(self.post('publication', **{'publication-premiere_date': str(today),
                                                     'publication-youtube_url': 'https://youtu.be/film',
                                                     'publication-hearthis_url': ''}).status_code, 302)
        self.assertEqual(self.client.get(self.public).context['page_obj'].paginator.count, 0)
        table = html.fromstring(self.client.get(self.list_url).content)
        self.assertEqual(len(table.xpath('//a[@href="https://youtu.be/film"]')), 1)
        self.assertEqual(self.client.get(self.list_url, {'status': 'published'}).context['page_obj'].paginator.count, 1)
        # Powrót do „Korekty” otwiera nową rundę do przejęcia; jej porzucenie usuwa nieprzejętą rundę.
        self.set_status('proofreading')
        self.assertIsNone(self.audio().proofreader_id)
        self.set_status('editing')
        self.assertEqual(AudiobookStage.objects.filter(stage_type='proofreading').count(), 1)

    @override_settings(DISCORD_WEBHOOKS={'Testowy': 'https://discord.com/api/webhooks/1/dawny-kanal',
                                         'Kanał': 'https://discord.com/api/webhooks/2/reguly'})
    def test_production_is_logged_without_discord_messages(self):
        """Zmiany statusu, przejęcie i zakończenie korekty audiobooka trafiają do dziennika (WorkflowEvent), ale nie
        wysyłają nic na Discord – także przy włączonych wszystkich regułach i skonfigurowanym dawnym kanale."""
        for rule in NotificationRule.objects.all():
            rule.enabled, rule.channel = True, 'Kanał'
            rule.save()
            rule.templates.create(content='[etap]: [tytuł]')
        with patch('core.discord_webhook.send_message', return_value='1') as sent, \
                self.captureOnCommitCallbacks(execute=True):
            self.to_proofreading()
            self.claim(self.reader)
            self.finish(self.reader)
        self.assertEqual(list(WorkflowEvent.objects.filter(kind='audiobook', text=self.text)
                              .order_by('pk').values_list('details', flat=True))[2:],
                         ['Przejęto korektę audiobooka.', 'Zakończono korektę audiobooka. Teraz: Nanoszenie poprawek.'])
        self.assertEqual(WorkflowEvent.objects.filter(kind='audiobook', text=self.text).count(), 4)
        sent.assert_not_called()
        self.assertFalse(DiscordNotification.objects.exists())

    def test_status_and_date_validation_stale_and_unknown_actions(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        for status, day in (('recording', tomorrow), ('unknown', None)):
            with self.subTest(status=status):
                self.assertEqual(self.set_status(status, day=day).status_code, 400)
        self.assertFalse(AudiobookStage.objects.exists())
        self.set_status('recording', day=timezone.localdate() - timedelta(days=1))
        # Zakończenie nie może poprzedzać rozpoczęcia.
        self.assertEqual(self.set_status('proofreading', day=timezone.localdate() - timedelta(days=3)).status_code, 400)
        self.to_proofreading()
        self.assign(self.reader)
        self.start(self.reader)
        stale = self.token()
        for stage_id in (999999, 'abc', '', '１', '1' * 19):
            self.assertEqual(self.post('finish_stage', stage_id=stage_id).status_code, 400)
        self.assertEqual(self.finish().status_code, 302)
        self.assertEqual(self.client.post(self.url, {'action': 'status', '_edit_version': stale}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'action': 'status'}).status_code, 409)
        for action in ('queue', 'pause'):
            self.assertEqual(self.post(action).status_code, 400)
        self.assertEqual(AudiobookStage.objects.count(), 3)

    def test_coordinator_corrects_stage_dates(self):
        """Daty etapów poprawia się w tabeli etapów (akcja „stages”): walidacja, uprawnienia, nieznana data."""
        today = timezone.localdate()
        self.set_status('recording', day=today - timedelta(days=4))
        self.set_status('proofreading', day=today - timedelta(days=2))
        recording, proofreading = AudiobookStage.objects.order_by('position')
        page = html.fromstring(self.client.get(self.url).content)
        self.assertEqual(len(page.xpath('//form[input[@value="stages"]]//table//input[@type="date"]')), 3)  # trwa – bez końca
        base = {f'stages-start_{recording.pk}': str(today - timedelta(days=6)),
                f'stages-end_{recording.pk}': str(today - timedelta(days=3)), f'stages-start_{proofreading.pk}': ''}
        rejected = {
            'end before start': {f'stages-end_{recording.pk}': str(today - timedelta(days=7))},
            'future date': {f'stages-start_{recording.pk}': str(today + timedelta(days=1))},
        }
        for case, change in rejected.items():
            with self.subTest(case):
                self.assertEqual(self.post('stages', **{**base, **change}).status_code, 400)
        self.assertEqual(self.post('stages', self.narrator, **base).status_code, 403)
        self.assertEqual(self.post('stages', **base).status_code, 302)
        self.assertEqual(stage_rows(self.text)[0], ('recording', today - timedelta(days=6), False,
                                                    today - timedelta(days=3), True))
        # Usunięcie daty nie cofa rozpoczęcia – data staje się nieznana; korektę może rozpocząć wpisana data.
        self.post('stages', **{**base, f'stages-start_{recording.pk}': '', f'stages-start_{proofreading.pk}': str(today)})
        self.assertEqual([row[1:3] for row in stage_rows(self.text)], [(None, True), (today, False)])
        self.assertTrue(self.audio().active_stage.is_started)

    def test_stage_table_is_editable_only_for_coordinators(self):
        """Koordynator widzi w tabeli etapów pola dat, listę wykonawcy korekty i przyciski „Usuń” (bez bieżącego
        etapu); korektor audiobooków widzi tabelę tylko do odczytu i dostaje 403 na akcję „stages”."""
        self.to_proofreading()
        self.assign(self.reader)
        self.start(self.reader)
        self.finish(self.reader)  # korekta zakończona, bieżący etap: nanoszenie poprawek
        recording, proofreading, corrections = AudiobookStage.objects.order_by('position')
        self.client.force_login(self.coordinator)
        page = html.fromstring(self.client.get(self.url).content)
        table = page.xpath('//form[input[@value="stages"]]//table')[0]
        self.assertEqual([b.get('value') for b in table.xpath('.//button[@name="delete_stage"]')],
                         [str(recording.pk), str(proofreading.pk)])
        self.assertEqual(len(table.xpath('.//input[@type="date"]')), 5)  # 2 zakończone etapy × 2 daty + start bieżącego
        options = table.xpath(f'.//select[@name="stages-performer_{proofreading.pk}"]/option')
        self.assertEqual(options[0].text, '– nie przypisano –')
        self.assertEqual({o.get('value') for o in options[1:]}, {str(self.reader.pk), str(self.other_reader.pk)})
        self.assertEqual([o.get('value') for o in options if o.get('selected') is not None], [str(self.reader.pk)])
        self.assertEqual(len(page.xpath('//button[text()="Zapisz zmiany"]')), 1)
        self.client.force_login(self.reader)
        page = html.fromstring(self.client.get(self.url).content)
        self.assertEqual(len(page.xpath('//table//input | //table//select | //button[@name="delete_stage"]')), 0)
        self.assertEqual(len(page.xpath('//table//tr')), 4)
        for fields in ({f'stages-start_{recording.pk}': ''}, {'delete_stage': recording.pk}):
            with self.subTest(fields=fields):
                self.assertEqual(self.post('stages', self.reader, **fields).status_code, 403)

    def test_coordinator_changes_proofreading_performer_from_the_table(self):
        """Zmiana wykonawcy korekty w tabeli: historycznej – tylko etap, bieżącej – także przypisany korektor;
        osoba bez roli „Korektor audiobooków” jest odrzucana, a dawny wykonawca zostaje na liście."""
        today = timezone.localdate()
        self.to_proofreading()
        self.assign(self.reader)
        self.start(self.reader)
        self.finish(self.reader)
        self.set_status('proofreading')  # druga korekta – bieżąca, do przejęcia
        self.assign(self.other_reader)
        first, second = AudiobookStage.objects.filter(stage_type='proofreading').order_by('position')
        data = {f'stages-start_{first.pk}': str(today), f'stages-end_{first.pk}': str(today),
                f'stages-performer_{first.pk}': self.reader.pk, f'stages-performer_{second.pk}': self.other_reader.pk}
        self.assertEqual(self.post('stages', **data).status_code, 302)
        self.assertEqual(self.post('stages', **{**data, f'stages-performer_{first.pk}': self.narrator.pk}).status_code, 400)
        events = self.text.workflowevent_set.count()
        # Historyczna korekta: zmiana wykonawcy nie dotyka przypisanego korektora bieżącej.
        self.assertEqual(self.post('stages', **{**data, f'stages-performer_{first.pk}': self.other_reader.pk}).status_code, 302)
        self.assertEqual((AudiobookStage.objects.get(pk=first.pk).performer, self.audio().proofreader),
                         (self.other_reader, self.other_reader))
        self.assertIn('Korekta: wykonawca', self.text.workflowevent_set.order_by('-pk').first().details)
        # Bieżąca korekta: wykonawca staje się przypisanym korektorem; „nie przypisano” zwalnia korektę.
        for performer, expected in ((self.reader, self.reader), (None, None)):
            with self.subTest(performer=performer):
                response = self.post('stages', **{**data, f'stages-performer_{first.pk}': self.other_reader.pk,
                                                  f'stages-performer_{second.pk}': performer.pk if performer else ''})
                self.assertEqual(response.status_code, 302)
                self.assertEqual((AudiobookStage.objects.get(pk=second.pk).performer, self.audio().proofreader),
                                 (expected, expected))
        self.assertEqual(self.text.workflowevent_set.count(), events + 3)
        # Dawny wykonawca bez roli zostaje w historii i na liście wyboru – ale nie można go wybrać dla innego etapu.
        self.other_reader.person_profile.roles.clear()
        self.assertEqual(self.post('stages', **{**data, f'stages-performer_{first.pk}': self.other_reader.pk,
                                                 f'stages-performer_{second.pk}': ''}).status_code, 302)
        page = html.fromstring(self.client.get(self.url).content)
        self.assertEqual([o.get('value') for o in page.xpath(f'//select[@name="stages-performer_{first.pk}"]/option')],
                         ['', str(self.reader.pk), str(self.other_reader.pk)])
        self.assertEqual([o.get('value') for o in page.xpath(f'//select[@name="stages-performer_{second.pk}"]/option')],
                         ['', str(self.reader.pk)])
        self.assertEqual(self.post('stages', **{**data, f'stages-performer_{first.pk}': self.other_reader.pk,
                                                 f'stages-performer_{second.pk}': self.other_reader.pk}).status_code, 400)

    def test_coordinator_deletes_stages_from_the_table_except_the_current_one(self):
        """Przycisk „Usuń” usuwa etap z historii (pozycje pozostałych bez zmian, reszta tabeli niezapisywana);
        bieżącego etapu nie można usunąć, a nieistniejący etap daje komunikat."""
        today = timezone.localdate()
        self.set_status('recording', day=today - timedelta(days=4))
        self.set_status('editing', day=today - timedelta(days=1))
        recording, editing = AudiobookStage.objects.order_by('position')
        rejected = {
            'bieżący etap': (editing.pk, 'Bieżący etap zamyka zmiana statusu – nie można go usunąć.'),
            'nieistniejący etap': (editing.pk + 1000, 'Tego etapu już nie ma.'),
            'nieprawidłowy identyfikator': ('abc', 'Wybierz prawidłowy etap.'),
        }
        for case, (stage_id, message) in rejected.items():
            with self.subTest(case):
                response = self.post('stages', delete_stage=stage_id)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, message, status_code=400)
        self.assertEqual(AudiobookStage.objects.count(), 2)
        events = self.text.workflowevent_set.count()
        response = self.post('stages', delete_stage=recording.pk, **{f'stages-start_{editing.pk}': str(today)})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual([(s.pk, s.position, s.started_at) for s in AudiobookStage.objects.all()],
                         [(editing.pk, editing.position, today - timedelta(days=1))])  # data z formularza pominięta
        self.assertEqual((self.audio().status, self.audio().active_stage_id), ('editing', editing.pk))
        event = self.text.workflowevent_set.order_by('-pk').first()
        self.assertEqual(self.text.workflowevent_set.count(), events + 1)
        self.assertEqual(event.details, f'Usunięto etap: Nagrywanie ({today - timedelta(days=4):%d.%m.%Y} – {today - timedelta(days=1):%d.%m.%Y}).')

    def test_disabled_and_withdrawn_preserve_history_and_forbid_mutations(self):
        self.to_proofreading()
        self.assign(self.reader)
        self.start()
        stage = self.audio().active_stage
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=True)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.list_url).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.queue_url).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.finish().status_code, 403)
        self.assertEqual(self.set_status('corrections').status_code, 403)
        self.assertEqual(self.assign(None).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=False, for_recording=False)
        self.assertEqual(self.finish().status_code, 403)
        Text.objects.filter(pk=self.text.pk).update(for_recording=True)
        WorkflowStage.objects.create(text=self.text, stage_type='withdrawn')
        self.assertEqual(self.finish().status_code, 403)
        self.assertEqual(self.audio().active_stage_id, stage.pk)
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        chapter = Text.objects.create(title='Rozdział 1', anthology=novel, chapter_number=1, length=1)
        self.assertEqual(self.client.get(reverse('core:audiobook_detail', args=[chapter.pk])).status_code, 404)

    def test_disabled_audiobook_metadata_editable_but_status_blocked(self):
        audio = Audiobook.objects.create(text=self.text, status='published')
        self.text.audiobook_blacklisted = True
        self.text.save()
        self.client.force_login(self.admin)
        page = self.client.get(self.url)
        self.assertTrue(page.context['can_edit'])
        self.assertFalse(page.context['can_start'] or page.context['can_finish'])
        self.assertNotContains(page, 'Zmień status')
        data = {'publication-premiere_date': '2020-05-04',
                'publication-youtube_url': 'https://www.youtube.com/watch?v=abcdefghijk',
                'publication-hearthis_url': ''}
        self.assertEqual(self.post('publication', **data).status_code, 302)
        self.assertEqual(self.post('notes', **{'notes-coordinator_notes': 'Nagranie z 2020 r.'}).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual((audio.premiere_date, audio.status, audio.coordinator_notes),
                         (date(2020, 5, 4), 'published', 'Nagranie z 2020 r.'))
        self.assertEqual(self.set_status('editing').status_code, 403)
        self.assertEqual(self.post('publication', self.member, **data).status_code, 403)

    def test_disabled_text_without_audio_cannot_create_metadata(self):
        self.text.for_recording = False
        self.text.save()
        self.client.force_login(self.admin)
        self.assertFalse(self.client.get(self.url).context['can_edit'])
        self.assertEqual(self.post('people', **{'people-new_narrator_name': 'Nowy lektor'}).status_code, 403)
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
        self.assertEqual(self.post('people', **{'people-new_narrator_name': 'Uzupełniony lektor'}).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual((audio.narrator_name, audio.status), ('Uzupełniony lektor', 'published'))

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
        self.assertContains(self.client.get(reverse('core:assigned_text_detail', args=[empty.pk])), 'Nieprzypisane')
        self.assertFalse(Audiobook.objects.filter(text=empty).exists())

    def test_text_card_links_to_audiobooks_only_for_people_with_access(self):
        """Karta tekstu pokazuje linki do Audiobooków tylko osobom, które mają do nich dostęp (bez linków na 403)."""
        url = reverse('core:assigned_text_detail', args=[self.text.pk])
        for user, visible in [(self.coordinator, True), (self.reader, True), (self.member, False)]:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                section = html.fromstring(response.content).get_element_by_id('text-audiobook')
                links = {a.get('href') for a in section.iter('a')}
                self.assertEqual({self.list_url, self.url} <= links, visible)
                self.assertEqual(bool(links & {self.list_url, self.url}), visible)

    def test_audiobook_events_do_not_change_text_status_sorting(self):
        self.set_status('recording')
        rows = text_list_context(user=self.admin, params={})['texts']
        self.assertIsNone(next(row for row in rows if row['pk'] == self.text.pk)['last_status_change'])

    def test_admin_preserves_status_and_corrects_history_dates(self):
        self.set_status('recording', day='')
        audio = self.audio()
        engineer = AudioContributor.objects.create(name='Montażysta')
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
        data = {'status': 'editing', 'engineer_contact': engineer.pk, 'proofreader': self.reader.pk,
                '_edit_version': form_value(page), '_save': 'Zapisz'}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual((audio.status, audio.engineer_name), ('recording', 'Montażysta'))
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
        # Trwający etap nie dostaje końca z panelu – kończy go zmiana statusu.
        self.assertEqual(self.client.post(history, {**fields, 'ended_at': '2026-01-06'}).status_code, 200)
        self.assertEqual(self.client.post(history, fields).status_code, 302)
        stage = AudiobookStage.objects.get(pk=audio.active_stage_id)
        self.assertEqual((stage.started_at, stage.start_unknown, stage.is_completed), (date(2026, 1, 5), False, False))
        self.assertEqual(self.audio().status, 'recording')

    def test_admin_edits_contacts_and_all_publication_parts_are_listed(self):
        parts = [{'service': 'youtube', 'part': 2, 'url': 'https://www.youtube.com/watch?v=second'},
                 {'service': 'hearthis', 'part': 2, 'url': 'https://hearthis.at/fantazmaty/second/'}]
        audio = Audiobook.objects.create(text=self.text, status='published', proofreader=self.reader,
                                         youtube_url='https://www.youtube.com/watch?v=first',
                                         hearthis_url='https://hearthis.at/fantazmaty/first/', additional_links=parts)
        narrator = AudioContributor.objects.create(name='Nowy lektor', email='new@example.test')
        self.client.force_login(self.admin)
        for url in (self.list_url, self.url):
            page = self.client.get(url)
            for link in (audio.youtube_url, audio.hearthis_url, *(part['url'] for part in parts)):
                self.assertContains(page, link)
            self.assertContains(page, 'YouTube – cz. 2')
        url = reverse('admin:core_audiobook_change', args=[audio.pk])
        response = self.client.post(url, {
            'narrator_contact': narrator.pk, 'proofreader': self.reader.pk, 'additional_links': json.dumps(parts),
            '_edit_version': form_value(self.client.get(url)), '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        audio.refresh_from_db()
        self.assertEqual((audio.narrator_name, audio.narrator_email, audio.engineer_name, audio.additional_links),
                         ('Nowy lektor', 'new@example.test', '', parts))
        # Puste pole dodatkowych linków zapisuje pustą listę.
        response = self.client.post(url, {'narrator_contact': narrator.pk, 'additional_links': '',
                                          '_edit_version': form_value(self.client.get(url)), '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.additional_links, [])
        audio.additional_links = [{'service': 'youtube', 'part': 2, 'url': 'javascript:evil'}]
        with self.assertRaises(ValidationError):
            audio.clean_fields()


class AudiobookModelValidationTests(AudiobookProductionTestData, TestCase):
    def errors(self, audio):
        with self.assertRaises(ValidationError) as caught:
            audio.clean()
        return caught.exception.message_dict

    def test_novel_chapters_and_moving_history_to_another_text_are_rejected(self):
        novel = Anthology.objects.create(title='Powieść audio', is_novel=True)
        chapter = Text.objects.create(title='Rozdział 1', anthology=novel, chapter_number=1, length=1)
        self.assertIn('Rozdziały powieści', self.errors(Audiobook(text=chapter))['text'][0])
        audio = Audiobook.objects.create(text=self.text)
        audio.text = Text.objects.create(title='Inny tekst', anthology=self.book, length=1)
        self.assertIn('Nie można przepiąć', self.errors(audio)['text'][0])

    def test_new_proofreader_must_be_eligible_but_historical_one_is_kept(self):
        audio = Audiobook(text=self.text, proofreader=self.member)
        self.assertIn('Korektor audiobooków', self.errors(audio)['proofreader'][0])
        audio.proofreader = self.reader
        audio.clean()
        # Zapisany wcześniej korektor, który stracił rolę, nie blokuje edycji innych pól.
        Audiobook.objects.filter(text=self.text).delete()
        audio = Audiobook.objects.create(text=self.text, proofreader=self.member)
        audio.clean()

    def test_audiobook_mirrors_directory_entry_even_on_partial_save(self):
        person = AudioContributor.objects.create(name='  Ewa   Głos ', email=' EWA@example.test ')
        audio = Audiobook.objects.create(text=self.text, status='recording', narrator_name='wpisane ręcznie')
        self.assertEqual(audio.narrator_name, '')  # osoba tylko ze spisu
        audio.narrator_contact = person
        audio.status = 'published'  # pole spoza update_fields nie jest zapisywane
        audio.save(update_fields=['narrator_contact'])
        stored = Audiobook.objects.get(pk=audio.pk)
        self.assertEqual((stored.narrator_name, stored.narrator_email, stored.status),
                         ('Ewa Głos', 'ewa@example.test', 'recording'))
        person.name = 'Ewa Nowak'
        person.save()
        self.assertEqual(Audiobook.objects.get(pk=audio.pk).narrator_name, 'Ewa Nowak')
