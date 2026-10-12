"""Panel admina CMS-u: etapy audiobooków, urlopy, skrzynki pocztowe, rekrutacja, Ekstrakty, notatki o autorach
oraz etykiety modeli – przypadki dotąd nieobjęte testami."""
from datetime import timedelta

from django.contrib.admin import site
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from authors.admin import AuthorAdmin, BlacklistedAuthorAdmin
from authors.models import Author, AuthorNote, BlacklistedAuthor
from core.admin import AudioContributorAdmin, VacationAdmin
from core.models import (AnthologyCorrection, AudioContributor, AudiobookStage, MailboxConnection, NewsletterConsent,
                         Recruitment, RecruitmentRoleDecision, ReplacementEntry, UserActivity)
from core.tests.people.support import admin_form_data
from fantazmaty.testing import create_superuser
from people.models import Person, Vacation
from texts.models import Anthology, Extract, Review, ReviewAssignment, Text


def without_templates(data):
    return {key: value for key, value in data.items() if '__prefix__' not in key}


class CoreAdminGapData(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('core-gaps')
        cls.today = timezone.localdate()

    def setUp(self):
        self.client.force_login(self.admin)

    def request(self, user=None):
        request = RequestFactory().post('/')
        request.user = user or self.admin
        request._messages = type('Messages', (), {'add': lambda *args, **kwargs: None})()
        return request


class AudiobookStageAdminTests(CoreAdminGapData):
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


class AudioContactMergeGapTests(CoreAdminGapData):
    def test_merge_keeps_own_email_of_oldest_profile(self):
        keep = AudioContributor.objects.create(name='Jan Lektor', email='jan@example.test')
        copy = AudioContributor.objects.create(name='Jan Lektor', email='inny@example.test')
        AudioContributorAdmin(AudioContributor, site).merge_contacts(
            self.request(), AudioContributor.objects.filter(pk__in=[keep.pk, copy.pk]))
        self.assertEqual(list(AudioContributor.objects.values_list('name', 'email')), [('Jan Lektor', 'jan@example.test')])


class VacationAdminGapTests(CoreAdminGapData):
    def test_add_page_and_post_without_person(self):
        url = reverse('admin:people_vacation_add')
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {'person': '', 'start_date': self.today.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertIn('person', response.context['adminform'].form.errors)
        self.assertFalse(Vacation.objects.exists())

    def test_started_vacation_cannot_be_deleted_even_when_permission_check_is_bypassed(self):
        person = Person.objects.create(first_name='Ola', last_name='Urlop')
        vacation = Vacation.objects.create(person=person, start_date=self.today)
        with self.assertRaisesMessage(PermissionDenied, 'jeszcze się nie rozpoczął'):
            VacationAdmin(Vacation, site).delete_model(self.request(), vacation)
        self.assertTrue(Vacation.objects.filter(pk=vacation.pk).exists())


class MailboxAdminGapTests(CoreAdminGapData):
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


class IntakeAdminGapTests(CoreAdminGapData):
    def test_recruitment_list_filters_by_role(self):
        editor = Recruitment.objects.create(first_name='Ewa', last_name='Redaktorka', email='ewa@example.com',
                                            department=Recruitment.Department.EDITORS)
        narrator = Recruitment.objects.create(first_name='Piotr', last_name='Lektor', email='piotr@example.com',
                                   department=Recruitment.Department.NARRATORS)
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


class AuthorNoteAdminGapTests(CoreAdminGapData):
    def test_editing_note_keeps_its_creator(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='jan@example.com')
        creator = get_user_model().objects.create_user('tworca-notatki')
        note = AuthorNote.objects.create(author=author, content='Pierwsza', created_by=creator)
        cases = {
            'strona notatki': (reverse('admin:authors_authornote_change', args=[note.pk]), 'authornote_form',
                               {'content': 'Z panelu notatek'}),
            'inline w autorze': (reverse('admin:authors_author_change', args=[author.pk]), 'author_form',
                                 {'notes-0-content': 'Z inline autora'}),
        }
        for case, (url, form_id, changes) in cases.items():
            with self.subTest(case):
                data = without_templates(admin_form_data(self.client, url, form_id, **changes))
                self.assertEqual(self.client.post(url, data).status_code, 302)
                note.refresh_from_db()
                self.assertEqual((note.content, note.created_by), (next(iter(changes.values())), creator))

    def test_author_views_refuse_account_without_superuser_even_if_site_check_is_bypassed(self):
        staff = get_user_model().objects.create_user('core-gaps-staff', is_staff=True)
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='jan@example.com')
        calls = {
            'zapis notatek autora': lambda: AuthorAdmin(Author, site).save_formset(
                self.request(staff), type('Form', (), {'instance': author})(), None, True),
            'dodanie do czarnej listy': lambda: BlacklistedAuthorAdmin(BlacklistedAuthor, site).add_view(
                self.request(staff)),
        }
        for case, call in calls.items():
            with self.subTest(case), self.assertRaises(PermissionDenied):
                call()


class ModelLabelTests(TestCase):
    def test_labels(self):
        book = Anthology.objects.create(title='Cienie')
        recruitment = Recruitment.objects.create(first_name='Ewa', last_name='Nowa', email='ewa@example.com',
                                                 department=Recruitment.Department.EDITORS)
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

    def test_recruitment_save_with_empty_update_fields_writes_nothing(self):
        recruitment = Recruitment.objects.create(first_name='Ewa', last_name='Nowa', email='ewa@example.com')
        recruitment.notes = 'niezapisane'
        recruitment.save(update_fields=[])
        recruitment.refresh_from_db()
        self.assertEqual(recruitment.notes, '')
