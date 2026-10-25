"""Narzędzia panelu administracyjnego: akcje zbiorcze, filtry list, dodawanie etapu, odtwarzanie fixture, dostęp
(także obrona w głębi SuperuserOnlyAdminMixin), skrzynki pocztowe i wybór tekstu nowego audiobooka."""
import json
from datetime import timedelta

from django.contrib.admin import site
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.core import serializers
from django.core.exceptions import PermissionDenied
from django.templatetags.static import static
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from authors.admin import AuthorAdmin
from authors.models import Author
from core.admin import AudioContributorAdmin
from core.edit_versions import version_of
from core.models import (AnthologyCorrection, AudioContributor, Audiobook, AudiobookStage, MailboxConnection,
                         NewsletterConsent, Recruitment, RecruitmentRoleDecision, ReplacementEntry, UserActivity)
from core.tests.people.support import admin_form_data
from fantazmaty.testing import create_member, create_superuser
from illustrations.models import Illustration, Illustrator
from people.models import Person
from texts.admin import ReviewAdmin, TextAdmin
from texts.models import Anthology, AnthologyTask, Extract, Review, ReviewAssignment, Text
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


class SuperuserOnlyAdminMixinTests(TestCase):
    """Obrona w głębi: gdyby strona admina (CMSAdminSite.has_permission) przepuściła konto bez superusera,
    uprawnienia modeli z SuperuserOnlyAdminMixin i oparte na nich sprawdzenia w widokach i akcjach nadal odmawiają."""

    ADMINS = ((TextAdmin, Text), (ReviewAdmin, Review), (AuthorAdmin, Author),
              (AudioContributorAdmin, AudioContributor))

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.admin = create_superuser('mixin-admin')
        cls.staff = users.create_user('mixin-staff', is_staff=True)
        cls.member = create_member('mixin-coordinator', 'Koordynator redakcji')
        cls.inactive_admin = create_superuser('mixin-inactive')
        users.filter(pk=cls.inactive_admin.pk).update(is_active=False)
        cls.inactive_admin.refresh_from_db()
        cls.text = Text.objects.create(title='Chroniony', length=10)
        cls.author = Author.objects.create(first_name='Anna', last_name='Prywatna', email='anna@example.com')

    def request(self, user, method='get'):
        request = getattr(RequestFactory(), method)('/')
        request.user, request.session = user, {}
        return request

    def test_only_active_superuser_gets_model_permissions_and_direct_calls(self):
        cases = ((self.staff, False), (self.member, False), (self.inactive_admin, False), (self.admin, True))
        permissions = ('has_module_permission', 'has_view_permission', 'has_add_permission',
                       'has_change_permission', 'has_delete_permission')
        for user, allowed in cases:
            request = self.request(user)
            for admin_class, model in self.ADMINS:
                model_admin = admin_class(model, site)
                for name in permissions:
                    with self.subTest(user=user.username, admin=admin_class.__name__, check=name):
                        self.assertIs(bool(getattr(model_admin, name)(request)), allowed)
            with self.subTest(user=user.username, check='akcje i widoki'):
                self.assertEqual('delete_selected' in ReviewAdmin(Review, site).get_actions(request), allowed)
                author_admin = AuthorAdmin(Author, site)
                self.assertEqual(author_admin.get_queryset(request).exists(), allowed)
                if allowed:
                    continue
                text_admin = TextAdmin(Text, site)
                for call in (lambda: text_admin.repeat_form(request, self.text.pk),
                             lambda: text_admin.block_audiobooks(self.request(user, 'post'), Text.objects.all()),
                             lambda: author_admin.history_view(request, str(self.author.pk))):
                    with self.assertRaises(PermissionDenied):
                        call()
        self.assertTrue(Text.objects.filter(pk=self.text.pk, audiobook_blacklisted=False).exists())
        self.assertEqual(AuthorAdmin(Author, site).history_view(self.request(self.admin), str(self.author.pk)).status_code,
                         200)


class AdminPanelData(TestCase):
    """Zalogowany superuser panelu i dzisiejsza data."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('panel-tools')
        cls.today = timezone.localdate()

    def setUp(self):
        self.client.force_login(self.admin)

class AudiobookStageAdminTests(AdminPanelData):
    def test_stage_dates_are_validated_and_save_without_start_invents_no_date(self):
        text = Text.objects.create(title='Nagranie', length=10)
        stage = AudiobookStage.objects.create(text=text, stage_type='recording', position=1, start_unknown=True,
                                              is_completed=True, ended_at=self.today - timedelta(days=1))
        url = reverse('admin:core_audiobookstage_change', args=[stage.pk])
        future = (self.today + timedelta(days=1)).isoformat()
        cases = {
            'data w przyszłości': ({'started_at': future}, 'started_at', 'Data nie może być w przyszłości.'),
            'koniec przed początkiem': ({'started_at': (self.today - timedelta(days=1)).isoformat(),
                                         'ended_at': (self.today - timedelta(days=3)).isoformat()},
                                        'ended_at', 'Zakończenie nie może poprzedzać rozpoczęcia.'),
        }
        for case, (changes, field, message) in cases.items():
            with self.subTest(case):
                response = self.client.post(url, admin_form_data(self.client, url, 'audiobookstage_form', **changes))
                self.assertIn(message, response.context['adminform'].form.errors[field])
        # Zapis bez daty rozpoczęcia nie wymyśla jej.
        queued = (self.today - timedelta(days=9)).isoformat()
        response = self.client.post(url, admin_form_data(self.client, url, 'audiobookstage_form', queued_at=queued))
        self.assertEqual(response.status_code, 302)
        stage.refresh_from_db()
        self.assertEqual((stage.queued_at.isoformat(), stage.started_at), (queued, None))


class MailboxAdminTests(AdminPanelData):
    """Skrzynki pocztowe w panelu: walidacja według przeznaczenia, hasło szyfrowane i zachowywane, sekcje IMAP/SMTP."""

    URL = reverse('admin:core_mailboxconnection_add')
    DATA = {'name': 'Rekrutacja', 'purpose': 'recruitment', 'host': 'imap.example.com', 'port': '993',
            'security': 'ssl', 'username': 'box@example.com', 'password': 'haslo', 'folder': 'INBOX',
            'is_active': 'on', '_save': 'Zapisz'}

    def test_invalid_connection_data_is_rejected(self):
        cases = {
            'brak hasła': ({'password': ''}, 'password', 'Podaj hasło skrzynki'),
            'adres z protokołem': ({'host': 'https://imap.example.com'}, 'host', 'bez protokołu'),
            'port poza zakresem': ({'port': '70000'}, 'port', 'od 1 do 65535'),
            'za długi nabór': ({'purpose': 'submissions', 'recruitment_subjects': 'x' * 256},
                               'recruitment_subjects', 'do 255 znaków'),
            'znak sterujący w folderze': ({'folder': 'IN\x01BOX'}, 'folder', 'Usuń znaki sterujące.'),
        }
        for case, (changes, field, message) in cases.items():
            with self.subTest(case):
                response = self.client.post(self.URL, {**self.DATA, **changes})
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, ' '.join(response.context['adminform'].form.errors[field]))
        self.assertFalse(MailboxConnection.objects.exists())

    def test_active_recruitment_mailbox_can_be_edited(self):
        self.assertEqual(self.client.post(self.URL, self.DATA).status_code, 302)
        box = MailboxConnection.objects.get()
        url = reverse('admin:core_mailboxconnection_change', args=[box.pk])
        page = self.client.get(url)
        response = self.client.post(url, {**self.DATA, 'password': '', 'folder': 'Kandydaci',
                                          '_edit_version': page.context['request'].edit_version_token})
        self.assertEqual(response.status_code, 302)
        box.refresh_from_db()
        self.assertEqual((box.folder, box.is_active, box.get_password()), ('Kandydaci', True, 'haslo'))

    SENDER = {'name': 'Wysyłka', 'purpose': 'split_mailing', 'username': 'wysylka@example.com', 'password': 'haslo',
              'is_active': 'on', 'smtp_host': 'smtp.example.com', 'smtp_port': '587', 'smtp_security': 'starttls',
              '_save': 'Zapisz'}

    def box(self, **fields):
        box = MailboxConnection(**{'name': 'Wysyłka', 'purpose': 'split_mailing', 'username': 'wysylka@example.com',
                                   'smtp_host': 'smtp.example.com', **fields})
        box.set_password('haslo')
        box.save()
        return box

    def test_validation_depends_on_purpose(self):
        """Wysyłka: serwer i port SMTP, login będący adresem; zgłoszenia i rekrutacja: serwer IMAP. Jedna aktywna wysyłkowa."""
        cases = {
            'wysyłka bez serwera SMTP': (self.SENDER, {'smtp_host': ''}, 'smtp_host', 'serwera SMTP bez protokołu'),
            'wysyłka – serwer z protokołem': (self.SENDER, {'smtp_host': 'smtps://smtp.example.com'}, 'smtp_host', 'bez protokołu'),
            'wysyłka – port SMTP 0': (self.SENDER, {'smtp_port': '0'}, 'smtp_port', 'od 1 do 65535'),
            'wysyłka – port SMTP za duży': (self.SENDER, {'smtp_port': '70000'}, 'smtp_port', 'od 1 do 65535'),
            'wysyłka – login bez adresu': (self.SENDER, {'username': 'wysylka'}, 'username', 'musi być adresem e-mail'),
            'zgłoszenia bez serwera IMAP': (self.DATA, {'purpose': 'submissions', 'host': ''}, 'host', 'serwera IMAP'),
            'rekrutacja bez serwera IMAP': (self.DATA, {'host': ''}, 'host', 'serwera IMAP'),
        }
        for case, (data, changes, field, message) in cases.items():
            with self.subTest(case):
                response = self.client.post(self.URL, {**data, **changes})
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, ' '.join(response.context['adminform'].form.errors[field]))
        self.assertFalse(MailboxConnection.objects.exists())
        self.box()
        response = self.client.post(self.URL, self.SENDER)
        self.assertIn('tylko jedna skrzynka „Wysyłka plików autorom”',
                      ' '.join(response.context['adminform'].form.errors['is_active']))
        self.assertEqual(self.client.post(self.URL, {**self.SENDER, 'name': 'Zapasowa', 'is_active': ''}).status_code, 302)

    def test_fields_of_other_protocol_are_optional_and_reset(self):
        """Skrzynka wysyłkowa zapisuje się bez pól IMAP, zgłoszeń – bez SMTP; pola drugiego protokołu wracają do domyślnych."""
        imap = {'host': 'imap.example.com', 'port': '143', 'security': 'starttls', 'folder': 'Wysłane',
                'recruitment_subjects': 'Nabór'}
        defaults = {'host': '', 'port': 993, 'security': 'ssl', 'folder': 'INBOX', 'recruitment_subjects': '',
                    'smtp_host': '', 'smtp_port': 465, 'smtp_security': 'ssl'}
        cases = {
            'wysyłka bez pól IMAP': ({**self.SENDER}, {'smtp_host': 'smtp.example.com', 'smtp_port': 587, 'smtp_security': 'starttls'}),
            'wysyłka z wypełnionymi polami IMAP': ({**self.SENDER, **imap}, {'smtp_host': 'smtp.example.com', 'smtp_port': 587,
                                                                               'smtp_security': 'starttls'}),
            'zgłoszenia z błędnymi polami SMTP': ({**self.DATA, 'purpose': 'submissions', 'smtp_host': 'smtp/x', 'smtp_port': '0'},
                                                  {'host': 'imap.example.com'}),
            'zgłoszenia bez portu i folderu': ({**self.DATA, 'purpose': 'submissions', 'port': '', 'security': '', 'folder': ''},
                                               {'host': 'imap.example.com'}),
        }
        for case, (data, expected) in cases.items():
            with self.subTest(case):
                self.assertEqual(self.client.post(self.URL, data).status_code, 302)
                box = MailboxConnection.objects.get()
                self.assertEqual({name: getattr(box, name) for name in defaults}, {**defaults, **expected})
                self.assertEqual(box.get_password(), 'haslo')
                box.delete()

    def test_add_form_offers_sender_purpose_with_sections(self):
        """Formularz dodawania: przeznaczenie „Wysyłka plików autorom”, osobne sekcje IMAP i SMTP przełączane skryptem."""
        page = self.client.get(self.URL)
        sections = [(name, list(options['fields']), options.get('classes', ()))
                    for name, options in page.context['adminform'].fieldsets]
        self.assertEqual(sections, [
            (None, ['name', 'purpose', 'username', 'password', 'is_active'], ()),
            ('Odbiór poczty (IMAP)', ['host', 'port', 'security', 'folder', 'recruitment_subjects'], ('mailbox-imap',)),
            ('Wysyłka plików autorom (SMTP)', ['smtp_host', 'smtp_port', 'smtp_security'], ('mailbox-smtp',)),
        ])
        self.assertContains(page, '<option value="split_mailing">Wysyłka plików autorom</option>', html=True)
        self.assertContains(page, static('core/mailbox-admin.js'))
        self.box()
        changelist = self.client.get(reverse('admin:core_mailboxconnection_changelist'))
        self.assertContains(changelist, 'smtp.example.com')

    def test_mailbox_choice_by_purpose(self):
        """Import zgłoszeń, rekrutacja i wysyłka wybierają skrzynki po przeznaczeniu – skrzynka wysyłkowa nie zastępuje importu."""
        from core.services.mailbox import MailboxError
        from core.services.mailbox_import import default_mailbox
        from core.services.mailbox_snapshots import recruitment_connection
        from core.services.split_mailing import MailingError, mailing_mailbox

        texts = self.box(name='teksty', purpose='submissions', host='imap.example.com', username='teksty@example.com',
                         smtp_host='')
        recruitment = self.box(name='Rekrutacja', purpose='recruitment', host='imap.example.com', smtp_host='')
        sender = self.box(name='teksty', username='teksty@example.org')
        self.assertEqual((default_mailbox(), recruitment_connection(), mailing_mailbox()), (texts, recruitment, sender))
        MailboxConnection.objects.filter(pk__in=[texts.pk, recruitment.pk]).update(is_active=False)
        for function, error in ((default_mailbox, MailboxError), (recruitment_connection, MailboxError)):
            with self.subTest(function.__name__), self.assertRaises(error):
                function()
        cases = {
            'brak aktywnej wysyłkowej': ({'is_active': False}, 'Brak skrzynki do wysyłki'),
            'dwie aktywne wysyłkowe': ({'is_active': True}, 'jest kilka'),
        }
        self.box(name='Druga', is_active=False)
        for case, (changes, message) in cases.items():
            with self.subTest(case):
                MailboxConnection.objects.filter(purpose='split_mailing').update(**changes)
                with self.assertRaisesMessage(MailingError, message):
                    mailing_mailbox()

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


class IntakeAdminTests(AdminPanelData):
    """Listy rekrutacji i Ekstraktów oraz strony rekordów zarządzanych przez usługi."""

    def test_recruitment_list_filters_by_role(self):
        editor = Recruitment.objects.create(first_name='Ewa', last_name='Redaktorka', email='ewa@example.com',
                                            mail_roles=['editors'])
        narrator = Recruitment.objects.create(first_name='Piotr', last_name='Lektor', email='piotr@example.com',
                                   mail_roles=['narrators'])
        url = reverse('admin:core_recruitment_changelist')
        cases = {'znana rola': ('editors', {editor}), 'nieznana rola – bez filtrowania': ('nieznana', {editor, narrator})}
        for case, (role, expected) in cases.items():
            with self.subTest(case):
                response = self.client.get(url, {'recruitment_role': role})
                self.assertEqual(set(response.context['cl'].result_list), expected)

    def test_extract_list_shows_titles_line_by_line(self):
        author = Author.objects.create(first_name='Ewa', last_name='Ekstrakt', email='ewa@example.com')
        Extract.objects.create(author=author, full_name='Ewa Ekstrakt', email='ewa@example.com',
                               title='Pierwszy\nDrugi', accepted_titles='Pierwszy', recruitment='Ekstrakty 2026',
                               submission_dates='2026-01-02')
        response = self.client.get(reverse('admin:texts_extract_changelist'))
        self.assertContains(response, 'Pierwszy<br>Drugi<br>')
        self.assertContains(response, '2026-01-02<br>')
        self.assertContains(response, '<td class="field-rejected_display">–</td>', html=True)

    def test_service_owned_assignment_and_replacement_pages_open(self):
        book = Anthology.objects.create(title='Nabór')
        review = Review.objects.create(title='Zgłoszenie', anthology=book, length=10, email='a@example.com',
                                       author_first_name='Jan', author_last_name='Autor')
        reviewer = get_user_model().objects.create_user('recenzent-gaps', first_name='Rita', last_name='Recenzent')
        assignment = ReviewAssignment.objects.create(review=review, user=reviewer, position=1)
        page = self.client.get(reverse('admin:texts_reviewassignment_change', args=[assignment.pk]))
        form = page.context['adminform'].form
        self.assertEqual(set(form.fields), {'notes'})
        entries = ReplacementEntry.objects.count()
        page = self.client.get(reverse('admin:core_replacemententry_add'))
        self.assertTrue(page.context['adminform'].form.allow_patterns)
        self.assertEqual(ReplacementEntry.objects.count(), entries)


class ModelLabelTests(TestCase):
    """Etykiety rekordów pokazywane w panelu (nagłówki stron zmian, linki, dziennik)."""

    def test_labels(self):
        book = Anthology.objects.create(title='Cienie')
        recruitment = Recruitment.objects.create(first_name='Ewa', last_name='Nowa', email='ewa@example.com',
                                                 mail_roles=['editors'])
        consent = NewsletterConsent.objects.create(email='czytelnik@example.com', premieres=True)
        cases = {
            'uwaga do antologii': (AnthologyCorrection(anthology=book, story_title='Latarnik'), 'Cienie – Latarnik'),
            'decyzja dla roli': (RecruitmentRoleDecision.objects.get(recruitment=recruitment), 'Redakcja'),
            'aktywność': (UserActivity(actor='ewa@example.com', action='Zapis tekstu'), 'ewa@example.com: Zapis tekstu'),
            'zgoda newsletterowa': (consent, f'Zgody newsletterowe #{consent.pk}'),
        }
        for case, (obj, label) in cases.items():
            with self.subTest(case):
                self.assertEqual(str(obj), label)
