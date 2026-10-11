"""Narzędzia panelu administracyjnego: akcje zbiorcze, filtry list, dodawanie etapu, odtwarzanie fixture, dostęp,
skrzynki pocztowe i wybór tekstu nowego audiobooka."""
import json

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.core import serializers
from django.test import TestCase
from django.urls import reverse

from core.edit_versions import version_of
from core.models import Audiobook, MailboxConnection, NewsletterConsent
from fantazmaty.testing import create_member, create_superuser
from illustrations.models import Illustration, Illustrator
from people.models import Person
from texts.models import Anthology, AnthologyTask, Text
from workflow.models import WorkflowStage


class AdminToolsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_superuser('tools')
        cls.book = Anthology.objects.create(title='Narzędzia')
        cls.story = Text.objects.create(title='Nagranie', anthology=cls.book, length=100)
        cls.other = Text.objects.create(title='Bez zmian', anthology=cls.book, length=100)
        cls.url = reverse('admin:texts_text_changelist')

    def setUp(self):
        self.client.force_login(self.user)

    def test_bulk_blacklist_preserves_unselected_and_records_history_and_revision(self):
        initial = version_of(self.story)
        response = self.client.post(self.url, {
            'action': 'block_audiobooks', '_selected_action': [self.story.pk], 'index': '0',
        })
        self.assertEqual(response.status_code, 302)
        self.story.refresh_from_db()
        self.other.refresh_from_db()
        self.assertTrue(self.story.audiobook_blacklisted)
        self.assertFalse(self.story.for_recording)
        self.assertFalse(self.other.audiobook_blacklisted)
        self.assertTrue(self.other.for_recording)
        self.assertGreater(version_of(self.story), initial)
        self.assertEqual(LogEntry.objects.filter(object_id=str(self.story.pk), action_flag=CHANGE).count(), 1)
        page = self.client.get(self.url, {'audiobook_blacklisted__exact': '1'})
        self.assertEqual([obj.pk for obj in page.context['cl'].result_list], [self.story.pk])
        response = self.client.post(self.url, {
            'action': 'unblock_audiobooks', '_selected_action': [self.story.pk, self.other.pk], 'index': '0',
        })
        self.assertEqual(response.status_code, 302)
        self.story.refresh_from_db()
        self.other.refresh_from_db()
        self.assertFalse(self.story.audiobook_blacklisted)
        self.assertFalse(self.story.for_recording)
        self.assertTrue(self.other.for_recording)

    def test_staff_without_superuser_cannot_open_panel_or_run_actions(self):
        staff = get_user_model().objects.create_user('staff-tools', is_staff=True)
        self.client.force_login(staff)
        page = self.client.get(reverse('admin:index'))
        self.assertEqual(page.status_code, 302)
        self.assertIn(reverse('admin:login'), page.url)
        response = self.client.post(self.url, {'action': 'block_audiobooks', '_selected_action': self.story.pk})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('admin:login'), response.url)
        self.story.refresh_from_db()
        self.assertFalse(self.story.audiobook_blacklisted)

    def test_illustrator_directory_uses_independent_active_and_inactive_contacts(self):
        contacts = [Illustrator.objects.create(first_name='Ilustrator', last_name=str(active), is_active=active)
                    for active in (True, False)]
        Person.objects.create(first_name='Inna', last_name='Osoba', email='inna@example.test')
        for filters, expected in (({'is_active__exact': '1'}, [contacts[0].pk]),
                                  ({'is_active__exact': '0'}, [contacts[1].pk]),
                                  ({}, [c.pk for c in contacts])):
            page = self.client.get(reverse('admin:illustrations_illustrator_changelist'), filters)
            self.assertEqual(page.status_code, 200)
            self.assertCountEqual([p.pk for p in page.context['cl'].result_list], expected)
            self.assertIn('is_active', page.context['cl'].list_display)

    def test_task_filter_and_record_page_size(self):
        url = reverse('admin:texts_anthologytask_changelist')
        response = self.client.get(url, {'task_type__exact': 'audio_description', 'status__exact': 'not_commissioned'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['cl'].result_list.values_list('task_type', flat=True)),
                         [AnthologyTask.TaskType.AUDIO_DESCRIPTION])
        response = self.client.get(self.url, {'page_size': '500'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['cl'].list_per_page, 500)

    def test_add_stage_page_and_newsletter_copy(self):
        performer = create_member('u0', 'Redaktor')
        loose = Text.objects.create(title='Bez antologii', length=100)
        url = reverse('admin:texts_text_add_stage', args=[loose.pk])
        self.assertContains(self.client.get(url), 'Dodaj etap')
        response = self.client.post(url, {'kind': 'editing', 'performer': performer.pk,
                                          'version': version_of(loose)})
        self.assertEqual(response.status_code, 302)
        NewsletterConsent.objects.create(email='test@example.test', premieres=True)
        response = self.client.get(reverse('core:newsletter_list'), {'page_size': 250})
        self.assertContains(response, 'Kopiuj adresy z tej strony')
        self.assertContains(response, 'data-copy-table')
        self.assertEqual(response.context['page_obj'].paginator.per_page, 250)

    def test_fixture_restore_keeps_ids_and_does_not_generate_related_records(self):
        """Chroni przed tworzeniem zadań i ilustracji przez sygnały podczas loaddata (surowy zapis)."""
        payload = json.dumps([
            {'model': 'texts.anthology', 'pk': 9999,
             'fields': {'title': 'Fixture', 'status': 'in_preparation', 'has_illustrations': True}},
            {'model': 'texts.text', 'pk': 9999,
             'fields': {'title': 'Fixture tekst', 'anthology': 9999, 'length': 100}},
            {'model': 'texts.anthologytask', 'pk': 20000,
             'fields': {'anthology': 9999, 'task_type': 'blurb', 'status': 'not_commissioned',
                        'assigned_to': None, 'commissioned_at': None}},
        ])
        for obj in serializers.deserialize('json', payload):
            obj.save()
        self.assertEqual(list(AnthologyTask.objects.filter(anthology_id=9999).values_list('pk', flat=True)),
                         [20000])
        self.assertFalse(Illustration.objects.filter(text_id=9999).exists())
        anthology = Anthology.objects.get(pk=9999)
        anthology.save()
        self.assertSetEqual(
            set(AnthologyTask.objects.filter(anthology=anthology).values_list('task_type', flat=True)),
            set(AnthologyTask.TaskType.values),
        )
        self.assertTrue(AnthologyTask.objects.filter(pk=20000).exists())
        self.assertTrue(Illustration.objects.filter(text_id=9999).exists())

    def test_mailbox_password_is_encrypted_and_kept_when_left_empty(self):
        add = reverse('admin:core_mailboxconnection_add')
        data = {'name': 'Zgłoszenia', 'purpose': 'submissions', 'host': 'imap.example.com', 'port': '993',
                'security': 'ssl', 'username': 'box@example.com', 'password': 'pierwsze-haslo', 'folder': 'INBOX',
                'recruitment_subjects': '', 'is_active': 'on', '_save': 'Zapisz'}
        self.assertEqual(self.client.post(add, data).status_code, 302)
        box = MailboxConnection.objects.get()
        self.assertNotIn('pierwsze-haslo', box.encrypted_password)
        self.assertEqual(box.get_password(), 'pierwsze-haslo')
        change = reverse('admin:core_mailboxconnection_change', args=[box.pk])
        for password, expected in (('', 'pierwsze-haslo'), ('nowe-haslo', 'nowe-haslo')):
            with self.subTest(password=password):
                page = self.client.get(change)
                self.assertNotContains(page, expected)
                response = self.client.post(change, {**data, 'password': password, 'folder': 'Archiwum',
                                                     '_edit_version': page.context['request'].edit_version_token})
                self.assertEqual(response.status_code, 302)
                box.refresh_from_db()
                self.assertEqual((box.folder, box.get_password()), ('Archiwum', expected))

    def test_mailbox_add_form_follows_recruitment_filter_from_list(self):
        url = reverse('admin:core_mailboxconnection_add')
        for filters, purpose, subjects_field in (('purpose__exact=recruitment', 'recruitment', False),
                                                 ('purpose__exact=submissions', 'submissions', True),
                                                 ('', 'submissions', True)):
            with self.subTest(filters=filters):
                page = self.client.get(url, {'_changelist_filters': filters} if filters else {})
                form = page.context['adminform'].form
                self.assertEqual(form['purpose'].value(), purpose)
                self.assertEqual('recruitment_subjects' in form.fields, subjects_field)

    def test_new_audiobook_offers_only_texts_waiting_for_recording(self):
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        abandoned = Anthology.objects.create(title='Porzucona', status='abandoned')
        excluded = {
            'nie do nagrania': Text.objects.create(title='Bez nagrania', anthology=self.book, length=1,
                                                   for_recording=False),
            'czarna lista': Text.objects.create(title='Zablokowany', anthology=self.book, length=1,
                                                audiobook_blacklisted=True),
            'ma audiobook': Text.objects.create(title='Nagrany', anthology=self.book, length=1),
            'powieść': Text.objects.create(title='Rozdział', anthology=novel, length=1, chapter_number=1),
            'porzucona antologia': Text.objects.create(title='Porzucony', anthology=abandoned, length=1),
            'wycofany': Text.objects.create(title='Wycofany', anthology=self.book, length=1),
        }
        Audiobook.objects.create(text=excluded['ma audiobook'])
        WorkflowStage.objects.create(text=excluded['wycofany'], workflow_cycle=1, stage_type='withdrawn',
                                     is_current=True, is_released=True)
        queryset = self.client.get(reverse('admin:core_audiobook_add')).context['adminform'].form.fields['text'].queryset
        self.assertCountEqual(queryset, [self.story, self.other])
        response = self.client.post(reverse('admin:core_audiobook_add'),
                                    {'text': excluded['czarna lista'].pk, 'additional_links': '[]', '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('text', response.context['adminform'].form.errors)
        self.assertFalse(Audiobook.objects.filter(text=excluded['czarna lista']).exists())
