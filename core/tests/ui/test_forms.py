"""Formularze interfejsu: zbiorczy import recenzji (podgląd, token, ostrzeżenia, odmowy), zbiorcze akcje
korekty poskładowej, wybór folderu Dropbox na stronie tekstu oraz reguły walidacji formularzy
(daty etapów i urlopów, powiadomienie autora, notatki, akcje zbiorcze recenzji)."""
from datetime import time, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.forms import (AuthorNoteForm, AuthorNotificationForm, CompleteStageForm,
                        ReviewBulkImportForm,
                        VacationForm)
from core.models import MailboxConnection, PostLayoutAssignment
from core.post_layout import selection_token
from fantazmaty.testing import FormControls, create_member, create_superuser
from people.models import Person, Vacation
from texts.models import Anthology, Review, Text
from workflow.models import WorkflowStage


class BulkReviewImportFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_superuser("admin", "admin@example.com")
        cls.book = Anthology.objects.create(title="Nabór")
        MailboxConnection.objects.create(name="teksty", host="imap.example.com", username="teksty@example.com",
                                         encrypted_password="unused")

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=tmp.name)
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(self.user)
        self.url = reverse("core:review_bulk_submit")

    def parse(self, response):
        parser = FormControls(self.url)
        parser.feed(response.content.decode())
        return parser

    def test_initial_page_has_actual_bulk_controls(self):
        response = self.client.get(reverse("core:review_bulk_import"))
        self.assertContains(response, '<textarea name="records"')
        self.assertContains(response, 'id="id_bulk_anthology"')
        self.assertContains(response, 'id="id_bulk_records"')
        page = self.parse(response)
        self.assertIn(self.url, [f.get("action") for f in page.forms])
        self.assertTrue(any(b.get("value") == "preview" for b in page.buttons))
        self.assertTrue(any(b.get("value") == "import" and "disabled" in b for b in page.buttons))

    def test_html_preview_token_can_be_submitted(self):
        data = {
            "anthology": str(self.book.pk),
            "records": "Jan Autor;Test importu;fantasy;1000;;jan@example.com;",
            "import_action": "preview",
        }
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test importu")
        page = self.parse(response)
        self.assertTrue(page.fields.get("preview_token"))
        self.assertTrue(any(b.get("value") == "import" and "disabled" not in b for b in page.buttons))
        self.assertEqual(Review.objects.count(), 0)
        data.update(preview_token=page.fields["preview_token"], import_action="import")
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(Review.objects.count(), 1)

    def test_invalid_input_is_preserved_and_blocks_save(self):
        response = self.client.post(self.url, {
            "anthology": self.book.pk, "records": "Źle wklejony wiersz", "import_action": "preview",
        })
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Źle wklejony wiersz", status_code=400)
        self.assertContains(response, "Import nie został wykonany", status_code=400)
        buttons = self.parse(response).buttons
        self.assertTrue(any(b.get("value") == "import" and "disabled" in b for b in buttons))
        self.assertFalse(Review.objects.exists())

    def test_blacklist_confirmation_is_available_in_rendered_form(self):
        Author.objects.create(first_name="Jan", last_name="Autor", email="jan@example.com", is_blacklisted=True)
        data = {
            "anthology": self.book.pk,
            "records": "Jan Autor;Test ostrzeżenia;fantasy;1000;;jan@example.com;",
            "import_action": "preview",
        }
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Sprawdź zgłoszenia przed importem", status_code=400)
        page = self.parse(response)
        self.assertTrue(page.fields.get("submission_warnings_token"))
        data.update(confirm_submission_warnings="on",
                    submission_warnings_token=page.fields["submission_warnings_token"])
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        data.update(preview_token=self.parse(response).fields["preview_token"], import_action="import")
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertTrue(Review.objects.get().is_hidden)


class PostLayoutBulkActionsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = create_member('v68manager', 'Koordynator korekty')
        cls.reader = create_member('v68reader', 'Korektor poskładowy')
        cls.book = Anthology.objects.create(title='Porzucona antologia', status='abandoned')
        cls.text = Text.objects.create(title='Porzucony tekst', anthology=cls.book, length=1)
        cls.stage = WorkflowStage.objects.create(text=cls.text, stage_type='ready_for_editing', is_released=True)

    def assignment(self):
        return PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.reader,
                                                   page_from=1, page_to=4, created_by=self.manager)

    def test_bulk_delete_needs_confirmation_keeps_history_and_any_coordinator_may_delete(self):
        item = self.assignment()
        self.client.force_login(self.manager)
        url = reverse('core:post_layout')
        doc = html.fromstring(self.client.get(url).content)
        self.assertEqual(set(doc.xpath('//*[@data-bulk-action][@hidden]/@data-bulk-action')),
                         {'status', 'assign', 'delete'})
        self.assertTrue(doc.xpath('//script[contains(@src,"/post-layout.")]'))
        data = {'action': 'bulk', 'operation': 'delete', 'selected': [selection_token(self.manager, item)]}
        self.assertEqual(self.client.post(url, data).status_code, 400)
        self.assertEqual(self.client.post(url, {**data, 'confirm_delete': '1'}).status_code, 302)
        self.assertFalse(PostLayoutAssignment.objects.present().filter(pk=item.pk).exists())
        self.assertTrue(PostLayoutAssignment.objects.deleted().filter(pk=item.pk).exists())
        # Koordynator innego działu też może edytować i usuwać przydziały.
        coordinator = create_member('v68editor', 'Koordynator redakcji')
        item = self.assignment()
        self.client.force_login(coordinator)
        self.assertEqual(self.client.get(reverse('core:post_layout_edit', args=[item.pk])).status_code, 200)
        data = {'action': 'bulk', 'operation': 'delete', 'selected': [selection_token(coordinator, item)],
                'confirm_delete': '1'}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertFalse(PostLayoutAssignment.objects.present().filter(pk=item.pk).exists())


@override_settings(DROPBOX_CHOOSER_APP_KEY='test-public-app-key')
class DropboxChooserTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.admin = create_superuser('chooser-admin', 'chooser-admin@example.com')
        cls.member = users.objects.create_user('chooser-member', 'chooser-member@example.com', 'test')
        Person.objects.create(user=cls.member, first_name='Jan', last_name='Test', email=cls.member.email)
        cls.book = Anthology.objects.create(title='Test wyboru folderu')
        cls.text = Text.objects.create(title='Folder', length=100, anthology=cls.book,
                                       file_url='https://www.dropbox.com/sh/existing/link?dl=0')

    def setUp(self):
        self.url = reverse('core:assigned_text_detail', args=[self.text.pk])

    def test_superuser_sees_picker_and_get_does_not_change_link(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertContains(response, 'id="dropbox-folder-choose"')
        self.assertContains(response, 'data-app-key="test-public-app-key"')
        self.assertRegex(response.content.decode(), r'dropbox-folder-chooser(\.[0-9a-f]+)?\.js')  # także z nazwą z hashem (manifest)
        self.assertContains(response, 'Zapisz link')
        buttons = html.fromstring(response.content).xpath('//*[@id="dropbox-folder-choose"]/parent::div/button')
        self.assertEqual([button.text_content().strip() for button in buttons],
                         ['Wybierz folder Dropbox', 'Zapisz link'])
        self.text.refresh_from_db()
        self.assertEqual(self.text.file_url, 'https://www.dropbox.com/sh/existing/link?dl=0')

    def test_member_sees_link_but_no_picker_or_app_key(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url)
        self.assertContains(response, 'Otwórz folder Dropbox')
        self.assertNotContains(response, 'id="dropbox-folder-choose"')
        self.assertNotContains(response, 'test-public-app-key')
        self.assertNotContains(response, 'dropins.js')
        response = self.client.post(reverse('core:update_text_file', args=[self.text.pk]),
                                    {'file_url': 'https://www.dropbox.com/sh/changed/link'})
        self.assertEqual(response.status_code, 403)
        self.text.refresh_from_db()
        self.assertIn('/existing/', self.text.file_url)

    @override_settings(DROPBOX_CHOOSER_APP_KEY='')
    def test_unconfigured_picker_has_manual_fallback_and_no_external_script(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertContains(response, 'data-input-id="id_file_url" disabled')
        self.assertContains(response, 'Link nadal możesz wkleić ręcznie')
        self.assertNotContains(response, 'dropins.js')

    def test_ready_anthology_hides_picker_and_external_script(self):
        today = timezone.localdate()
        WorkflowStage.objects.create(text=self.text, stage_type='ready', is_completed=True,
                                     started_at=today, ended_at=today)
        self.book.status = 'ready'
        self.book.save()
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'id="dropbox-folder-choose"')
        self.assertNotContains(response, "dropins.js")


class FormValidationRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.person = Person.objects.create(first_name='Urlop', last_name='Testowy')
        cls.book = Anthology.objects.create(title='Nabór formularzy')
        cls.importer = create_superuser('import-admin')

    def test_stage_end_cannot_be_in_future_or_before_start(self):
        stage = SimpleNamespace(stage_type='editing_control', started_at=self.today - timedelta(days=2))
        form = CompleteStageForm(stage=stage)
        self.assertIn('send_to_proofreading', form.fields)
        self.assertEqual(form.fields['ended_at'].widget.attrs['min'], stage.started_at.isoformat())
        for ended_at, message in ((self.today + timedelta(days=1), 'przyszłą datą'),
                                  (self.today - timedelta(days=3), 'poprzedzać rozpoczęcia')):
            form = CompleteStageForm({'ended_at': ended_at.isoformat()}, stage=SimpleNamespace(
                stage_type='editing', started_at=stage.started_at))
            self.assertFalse(form.is_valid())
            self.assertIn(message, form.errors['ended_at'][0])
        self.assertTrue(CompleteStageForm({'ended_at': self.today.strftime('%d.%m.%Y')}).is_valid())

    def vacation(self, instance=None, **data):
        data = {key: value.isoformat() if hasattr(value, 'isoformat') else value for key, value in data.items()}
        return VacationForm(data, instance=instance or Vacation(person=self.person))

    def test_vacation_dates_stay_within_today_and_one_year(self):
        year = self.today + timedelta(days=366)
        for data, field in (({'start_date': self.today - timedelta(days=1), 'until_revoked': 'on'}, 'start_date'),
                            ({'start_date': year, 'until_revoked': 'on'}, 'start_date'),
                            ({'start_date': self.today, 'end_date': year}, 'end_date'),
                            ({'start_date': self.today, 'end_date': self.today - timedelta(days=1)}, 'end_date')):
            with self.subTest(data=data):
                form = self.vacation(**data)
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)
        form = self.vacation(start_date=self.today, end_date=self.today + timedelta(days=3))
        self.assertTrue(form.is_valid(), form.errors)
        end = timezone.localtime(form.cleaned_data['end_date'])
        self.assertEqual((end.date(), end.time()), (self.today + timedelta(days=3), time.max))
        form = self.vacation(start_date=self.today, end_date=self.today, until_revoked='on')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data['end_date'])

    def test_running_vacation_keeps_its_past_start_date(self):
        started = self.today - timedelta(days=5)
        running = Vacation.objects.create(person=self.person, start_date=started, until_revoked=True)
        form = self.vacation(running, start_date=started, until_revoked='on')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.fields['start_date'].widget.attrs['min'], started.isoformat())

    def test_author_notification_date_follows_checkbox_and_allowed_range(self):
        notified = self.today - timedelta(days=3)
        self.assertEqual(AuthorNotificationForm().initial['author_notified_at'], self.today)
        form = AuthorNotificationForm({'author_notified_at': notified.isoformat()})
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.cleaned_data['author_notified_at'])
        form = AuthorNotificationForm({'author_notified': 'on'})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['author_notified_at'], self.today)
        form = AuthorNotificationForm({'author_notified': 'on',
                                       'author_notified_at': (self.today - timedelta(days=15)).isoformat()})
        self.assertFalse(form.is_valid())
        self.assertIn('14 dni', form.errors['author_notified_at'][0])

    def test_notes_and_warnings_rules(self):
        self.assertIn('Wpisz treść', AuthorNoteForm({'content': '   '}).errors['content'][0])
        existing = Author.objects.create(first_name='Ala', last_name='Autor').notes.create(content='Stara')
        form = AuthorNoteForm({'content': '  Nowa  '}, instance=existing)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['content'], 'Nowa')

    def import_form(self, records, user=None, **data):
        return ReviewBulkImportForm({'anthology': self.book.pk, 'records': records, **data},
                                    user=user or self.importer)

    def test_review_import_refusals(self):
        line = 'Jan Autor;Tytuł {};fantasy;1000;;jan{}@example.com;'
        form = self.import_form(line.format(1, 1), user=create_member('import-member', 'Recenzent'))
        self.assertFalse(form.is_valid())
        self.assertIn('wyłącznie dla superusera', str(form.errors))
        form = self.import_form('\n'.join('Zły wiersz' for _ in range(25)))
        self.assertFalse(form.is_valid())
        self.assertEqual(len(form.errors['records']), 21)
        self.assertIn('Pozostałych błędów: 5.', form.errors['records'][-1])
        form = self.import_form('Jan Autor;Tytuł;fantasy;1000;jan@example.com;;Inny nabór')
        self.assertFalse(form.is_valid())
        self.assertIn('nie odpowiada wybranej antologii', str(form.errors['records']))
        duplicate = '\n'.join([line.format(1, 1), line.format(1, 1)])
        form = self.import_form(duplicate, submission_warnings_token='podrobiony', confirm_submission_warnings='on')
        self.assertFalse(form.is_valid())
        self.assertIn('confirm_submission_warnings', form.errors)
        self.assertTrue(any('możliwy duplikat' in warning for warning in form.import_warnings))
