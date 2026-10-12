"""Luki pokrycia audiodeskrypcji, spisu lektorów i drobnych modeli (przydziały poskładowej, ustawienia programów)."""
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.models import AudioContributor, AudioDescription, PostLayoutAssignment, ProgramPreferences
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, AnthologyTask


class AudioDescriptionTaskSyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.writer = create_member('audiodeskrypcja', 'Redaktor').person_profile

    def book(self, title, **task):
        book = Anthology.objects.create(title=title)
        AudioDescription.objects.filter(anthology=book).delete()
        AnthologyTask.objects.filter(anthology=book, task_type='audio_description').update(**task)
        return book

    def test_new_description_of_finished_task_starts_completed(self):
        book = self.book('Gotowa', status='ready', assigned_to=self.writer)
        description = AudioDescription.objects.create(anthology=book)
        self.assertEqual(description.stage, AudioDescription.Stage.COMPLETED)

    def test_completion_without_writer_is_refused_on_save(self):
        book = self.book('Bez wykonawcy', assigned_to=None, status='not_commissioned')
        description = AudioDescription.objects.create(anthology=book)
        description.stage = AudioDescription.Stage.COMPLETED
        with self.assertRaisesMessage(ValidationError, 'Zakończenie wymaga przypisania osoby piszącej'):
            description.save()
        self.assertEqual(AudioDescription.objects.get(pk=description.pk).stage, AudioDescription.Stage.WRITING)

    def test_admin_list_marks_missing_task(self):
        book = self.book('Bez zadania')
        description = AudioDescription.objects.create(anthology=book)
        AnthologyTask.objects.filter(anthology=book, task_type='audio_description').delete()
        model_admin = admin.site._registry[AudioDescription]
        self.assertEqual(model_admin.task_link(description), 'Brak zadania')


class AudioContributorConflictTests(TestCase):
    def test_stale_or_forged_version_keeps_entry_unchanged(self):
        admin_user = create_superuser('lektorzy-admin')
        self.client.force_login(admin_user)
        person = AudioContributor.objects.create(name='Lektor Pierwszy', email='lektor@example.test')
        response = self.client.post(reverse('core:audio_contributor', args=[person.pk]),
                                    {'name': 'Nowe imię', 'email': 'lektor@example.test', 'is_active': 'on',
                                     'version': 'podrobiony'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('Dane zmieniły się lub formularz wygasł', str(response.context['form'].non_field_errors()))
        person.refresh_from_db()
        self.assertEqual(person.name, 'Lektor Pierwszy')


class SmallModelGapTests(TestCase):
    def test_post_layout_page_range_and_program_settings_label(self):
        user = create_member('poskladowa', 'Korektor poskładowy')
        book = Anthology.objects.create(title='Złożona')
        for page_from, page_to in ((0, 3), (5, 3)):
            with self.subTest(pages=(page_from, page_to)):
                item = PostLayoutAssignment(anthology=book, proofreader=user, created_by=user,
                                            page_from=page_from, page_to=page_to)
                with self.assertRaises(ValidationError) as error:
                    item.clean()
                self.assertIn('page_to', error.exception.message_dict)
        self.assertEqual(str(ProgramPreferences(user=user)), 'Ustawienia programów: poskladowa')
