"""Produkcja audiobooków: zabezpieczenia usług (uprawnienia, stan etapu), poprawki dat bez zmian i z nieznanym
początkiem, korekta prowadzona bez korektora oraz etykiety korektorów – przypadki dotąd nieobjęte testami."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.audiobook_services import delete_stage, finish_stage, set_status, start_stage, update_stage_dates
from core.models import Audiobook, AudiobookStage
from fantazmaty.testing import create_member, edit_token
from texts.models import Anthology, Text

from .support import AudiobookProductionTestData


class AudiobookServiceGuardTests(AudiobookProductionTestData, TestCase):
    def test_service_refusals_leave_stages_unchanged(self):
        """Usługi odmawiają także przy wywołaniu z pominięciem formularzy strony."""
        self.to_proofreading()
        self.assign(self.reader)
        audio = self.audio()
        recording = AudiobookStage.objects.get(text=self.text, stage_type='recording')
        cases = {
            'status zmienia tylko koordynator': (PermissionDenied, lambda: set_status(
                text_id=self.text.pk, user=self.reader, status='editing')),
            'nieznany status': (ValidationError, lambda: set_status(
                text_id=self.text.pk, user=self.coordinator, status='paused')),
            'korektę rozpoczyna przypisany korektor': (PermissionDenied, lambda: start_stage(
                text_id=self.text.pk, user=self.other_reader)),
            'korektę kończy przypisany korektor': (PermissionDenied, lambda: finish_stage(
                text_id=self.text.pk, stage_id=audio.active_stage_id, user=self.other_reader)),
            'zakończenie etapu innego niż korekta': (ValidationError, lambda: finish_stage(
                text_id=self.text.pk, stage_id=recording.pk, user=self.coordinator)),
            'koniec trwającego etapu w poprawce dat': (ValidationError, lambda: update_stage_dates(
                text_id=self.text.pk, user=self.coordinator,
                dates={audio.active_stage_id: (timezone.localdate(), timezone.localdate())})),
            'wykonawca korekty bez roli': (ValidationError, lambda: update_stage_dates(
                text_id=self.text.pk, user=self.coordinator, dates={}, performers={audio.active_stage_id: self.narrator.pk})),
            'etapy usuwa tylko koordynator': (PermissionDenied, lambda: delete_stage(
                text_id=self.text.pk, user=self.reader, stage_id=recording.pk)),
            'usunięcie bieżącego etapu': (ValidationError, lambda: delete_stage(
                text_id=self.text.pk, user=self.coordinator, stage_id=audio.active_stage_id)),
        }
        before = list(AudiobookStage.objects.filter(text=self.text).values())
        for case, (error, call) in cases.items():
            with self.subTest(case), self.assertRaises(error):
                call()
        self.assertEqual(list(AudiobookStage.objects.filter(text=self.text).values()), before)
        self.assertEqual(self.audio().proofreader, self.reader)

    def test_finishing_non_proofreading_stage_is_refused(self):
        self.set_status('recording')
        audio = self.audio()
        with self.assertRaisesMessage(ValidationError, 'Pozostałe etapy kończy zmiana statusu.'):
            finish_stage(text_id=self.text.pk, stage_id=audio.active_stage_id, user=self.coordinator)
        self.assertEqual(self.audio().status, 'recording')

    def test_started_proofreading_cannot_be_started_again(self):
        self.to_proofreading()
        self.assign(self.reader)
        self.start(self.reader)
        with self.assertRaisesMessage(ValidationError, 'Ten etap już trwa.'):
            start_stage(text_id=self.text.pk, user=self.coordinator)

    def test_dates_cannot_be_corrected_before_audiobook_exists(self):
        with self.assertRaisesMessage(ValidationError, 'nie ma jeszcze etapów'):
            update_stage_dates(text_id=self.text.pk, user=self.coordinator, dates={})
        self.assertFalse(Audiobook.objects.filter(text=self.text).exists())

    def test_partial_date_correction_changes_only_listed_stages(self):
        today = timezone.localdate()
        self.set_status('recording', day=today - timedelta(days=5))
        self.set_status('editing', day=today - timedelta(days=2))
        recording, editing = AudiobookStage.objects.filter(text=self.text).order_by('position')
        changed = update_stage_dates(text_id=self.text.pk, user=self.coordinator,
                                     dates={recording.pk: (today - timedelta(days=6), today - timedelta(days=2))})
        self.assertEqual(changed, ['Nagrywanie'])
        editing_after = AudiobookStage.objects.get(pk=editing.pk)
        self.assertEqual((editing_after.started_at, editing_after.ended_at), (editing.started_at, editing.ended_at))


class AudiobookDatesPageTests(AudiobookProductionTestData, TestCase):
    def dates(self, stages, **changes):
        data = {}
        for stage in stages:
            data[f'stages-start_{stage.pk}'] = str(stage.started_at or '')
            if stage.is_completed:
                data[f'stages-end_{stage.pk}'] = str(stage.ended_at or '')
        return self.post('stages', **{**data, **changes})

    def test_unchanged_dates_and_new_end_of_stage_with_unknown_start(self):
        today = timezone.localdate()
        self.set_status('recording', day='')
        self.set_status('editing', day=today - timedelta(days=1))
        stages = list(AudiobookStage.objects.filter(text=self.text).order_by('position'))
        events = self.text.workflowevent_set.count()
        response = self.dates(stages)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertIn('Etapy bez zmian.', [str(m) for m in response.wsgi_request._messages])
        self.assertEqual(self.text.workflowevent_set.count(), events)
        recording = stages[0]
        self.assertEqual(self.dates(stages, **{f'stages-end_{recording.pk}': str(today - timedelta(days=3))}).status_code, 302)
        recording.refresh_from_db()
        self.assertEqual((recording.started_at, recording.start_unknown, recording.ended_at),
                         (None, True, today - timedelta(days=3)))
        self.assertEqual(self.text.workflowevent_set.count(), events + 1)

    def test_future_start_date_of_proofreading_is_rejected(self):
        self.to_proofreading()
        response = self.start(self.coordinator, day=timezone.localdate() + timedelta(days=1))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Data nie może być w przyszłości.', response.context['date_form'].errors['date'])
        self.assertFalse(self.audio().active_stage.is_started)


class ProofreadingWithoutProofreaderTests(AudiobookProductionTestData, TestCase):
    def test_coordinator_runs_proofreading_without_proofreader(self):
        """Korekta prowadzona przez koordynatora bez przypisanego korektora nie ma wykonawcy."""
        self.to_proofreading()
        self.assertEqual(self.start(self.coordinator).status_code, 302)
        self.assertEqual(self.finish(self.coordinator).status_code, 302)
        stage = AudiobookStage.objects.get(text=self.text, stage_type='proofreading')
        self.assertEqual((stage.is_completed, stage.performer_id), (True, None))
        self.assertEqual(self.audio().status, 'corrections')

    def test_status_change_credits_proofreader_assigned_after_start(self):
        self.to_proofreading()
        self.start(self.coordinator)
        Audiobook.objects.filter(text=self.text).update(proofreader=self.reader)
        self.assertEqual(self.set_status('editing').status_code, 302)
        stage = AudiobookStage.objects.get(text=self.text, stage_type='proofreading')
        self.assertEqual((stage.is_completed, stage.performer), (True, self.reader))


class ProofreaderLabelTests(AudiobookProductionTestData, TestCase):
    def test_admin_lists_proofreaders_by_profile_or_account_name(self):
        self.to_proofreading()
        former = get_user_model().objects.create_user('dawny-korektor', first_name='Dawny', last_name='Korektor')
        Audiobook.objects.filter(text=self.text).update(proofreader=former)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('admin:core_audiobook_change', args=[self.audio().pk]))
        choices = dict(page.context['adminform'].form.fields['proofreader'].choices)
        labels = {str(value): label for value, label in choices.items()}
        self.assertEqual(labels[str(former.pk)], 'Dawny Korektor')
        self.assertEqual(labels[str(self.reader.pk)], str(self.reader.person_profile))


class AudiobookSettingInvalidTextTests(TestCase):
    """Błąd danych tekstu w polu spoza formularza audiobooka (np. numer rozdziału poza powieścią)
    pokazuje komunikat zamiast błędu 500 i niczego nie zapisuje."""

    def test_error_on_field_outside_the_form_is_shown_as_general_error(self):
        member = create_member('koordynator-rozdzial', 'Koordynator redakcji')
        text = Text.objects.create(title='Rozdział', length=100, anthology=Anthology.objects.create(title='Zwykła'))
        Text.objects.filter(pk=text.pk).update(chapter_number=3, for_recording=False)
        text.refresh_from_db()
        self.client.force_login(member)
        response = self.client.post(reverse('core:update_text_audiobook', args=[text.pk]),
                                    {'for_recording': 'on', '_edit_version': edit_token(member, text)})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['audiobook_form'].non_field_errors())
        text.refresh_from_db()
        self.assertFalse(text.for_recording)
