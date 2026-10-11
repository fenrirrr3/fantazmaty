"""Zapisy na stronie tekstu poza tagami: notatki (dodanie, edycja, usunięcie – autor notatki albo
koordynator), notatka koordynatora, ostrzeżenia dotyczące treści i folder Dropbox; uprawnienia osób
przypisanych do bieżącego cyklu, odrzucane dane i rozdziały powieści."""
from tempfile import TemporaryDirectory

from django.test import TestCase
from django.urls import reverse

from fantazmaty.testing import create_member, create_superuser, edit_token
from texts.models import Anthology, Text, TextNote
from workflow.models import WorkflowRoleAssignment

NUL = 'zły\x00znak'  # Formularze odrzucają znak NUL – jedyna niepoprawna treść pól tekstowych.


class TextFieldsTestData:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('fields-admin')
        cls.coordinator = create_member('fields-coordinator', 'Koordynator redakcji')
        cls.editor = create_member('fields-editor', 'Redaktor')
        cls.other = create_member('fields-other', 'Redaktor')
        cls.book = Anthology.objects.create(title='Antologia pól')
        cls.text = Text.objects.create(title='Tekst z polami', anthology=cls.book, length=100,
                                       content_warnings='Przemoc', file_url='https://www.dropbox.com/sh/stary')
        WorkflowRoleAssignment.objects.create(text=cls.text, role='editor', assigned_to=cls.editor)
        cls.novel = Anthology.objects.create(title='Powieść pól', is_novel=True)
        cls.chapter = Text.objects.create(title='Rozdział', anthology=cls.novel, chapter_number=1, length=100)
        WorkflowRoleAssignment.objects.create(text=cls.chapter, role='editor', assigned_to=cls.editor)

    def setUp(self):
        super().setUp()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=tmp.name)
        override.enable()
        self.addCleanup(override.disable)
        self.detail = reverse('core:assigned_text_detail', args=[self.text.pk])

    def post(self, user, route, data, *, text=None, args=()):
        text = text or self.text
        self.client.force_login(user)
        return self.client.post(reverse(route, args=[text.pk, *args]),
                                {**data, '_edit_version': edit_token(user, text)})


class TextNotesTests(TextFieldsTestData, TestCase):
    def note(self, author, content='Notatka'):
        return TextNote.objects.create(text=self.text, author=author, content=content)

    def test_assigned_person_and_coordinator_add_notes_others_cannot(self):
        for user in (self.editor, self.coordinator):
            response = self.post(user, 'core:add_text_note', {'content': f'Od {user.username}', 'is_important': 'on'})
            self.assertRedirects(response, self.detail)
        notes = TextNote.objects.order_by('pk')
        self.assertEqual([(n.author, n.is_important) for n in notes], [(self.editor, True), (self.coordinator, True)])
        response = self.post(self.other, 'core:add_text_note', {'content': 'Obca notatka'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(TextNote.objects.count(), 2)

    def test_empty_note_is_rejected_on_text_page(self):
        response = self.post(self.editor, 'core:add_text_note', {'content': ''})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['text_note_form'].errors)
        self.assertFalse(TextNote.objects.exists())

    def test_note_author_and_coordinator_edit_note_but_not_other_people(self):
        note = self.note(self.editor)
        self.client.force_login(self.editor)
        url = reverse('core:edit_text_note', args=[self.text.pk, note.pk])
        page = self.client.get(url)
        self.assertContains(page, 'Treść notatki')
        self.assertContains(page, 'Notatka')
        for user, content in ((self.editor, 'Poprawiona'), (self.coordinator, 'Przez koordynatora')):
            response = self.post(user, 'core:edit_text_note', {'content': content}, args=[note.pk])
            self.assertRedirects(response, self.detail)
            note.refresh_from_db()
            self.assertEqual(note.content, content)
        self.assertEqual(self.post(self.other, 'core:edit_text_note', {'content': 'Obca'}, args=[note.pk]).status_code,
                         403)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 403)
        response = self.post(self.editor, 'core:edit_text_note', {'content': ''}, args=[note.pk])
        self.assertEqual(response.status_code, 400)
        note.refresh_from_db()
        self.assertEqual((note.content, note.author), ('Przez koordynatora', self.editor))

    def test_note_of_another_text_is_not_found(self):
        other_text = Text.objects.create(title='Inny', anthology=self.book, length=1)
        note = TextNote.objects.create(text=other_text, author=self.editor, content='Gdzie indziej')
        response = self.post(self.editor, 'core:edit_text_note', {'content': 'Zmiana'}, args=[note.pk])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.post(self.editor, 'core:delete_text_note', {}, args=[note.pk]).status_code, 404)

    def test_only_author_or_coordinator_deletes_note(self):
        own, foreign = self.note(self.editor), self.note(self.coordinator)
        self.assertEqual(self.post(self.other, 'core:delete_text_note', {}, args=[own.pk]).status_code, 403)
        self.assertEqual(self.post(self.editor, 'core:delete_text_note', {}, args=[foreign.pk]).status_code, 403)
        self.assertRedirects(self.post(self.editor, 'core:delete_text_note', {}, args=[own.pk]), self.detail)
        self.assertRedirects(self.post(self.coordinator, 'core:delete_text_note', {}, args=[foreign.pk]), self.detail)
        self.assertFalse(TextNote.objects.exists())


class TextFieldsEditingTests(TextFieldsTestData, TestCase):
    def test_coordinator_note_only_by_coordinator(self):
        response = self.post(self.coordinator, 'core:update_coordinator_note', {'coordinator_note': 'Pilne'})
        self.assertRedirects(response, self.detail)
        self.assertEqual(self.post(self.editor, 'core:update_coordinator_note', {'coordinator_note': 'X'}).status_code,
                         403)
        response = self.post(self.coordinator, 'core:update_coordinator_note', {'coordinator_note': NUL})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['coordinator_note_form'].errors)
        self.text.refresh_from_db()
        self.assertEqual(self.text.coordinator_note, 'Pilne')

    def test_content_warnings_by_assigned_person_and_not_for_chapters(self):
        response = self.post(self.editor, 'core:update_text_content_warnings', {'content_warnings': ' Przemoc, krew '})
        self.assertRedirects(response, self.detail)
        self.text.refresh_from_db()
        self.assertEqual(self.text.content_warnings, 'Przemoc, krew')
        for user, data, status in ((self.other, {'content_warnings': 'Obce'}, 403),
                                   (self.editor, {'content_warnings': NUL}, 400)):
            self.assertEqual(self.post(user, 'core:update_text_content_warnings', data).status_code, status)
        response = self.post(self.editor, 'core:update_text_content_warnings', {'content_warnings': 'Nowe'},
                             text=self.chapter)
        self.assertEqual(response.status_code, 403)
        self.text.refresh_from_db()
        self.chapter.refresh_from_db()
        self.assertEqual((self.text.content_warnings, self.chapter.content_warnings), ('Przemoc, krew', ''))

    def test_dropbox_folder_by_superuser_with_validation_and_not_for_chapters(self):
        link = 'https://www.dropbox.com/sh/nowy?dl=0'
        self.assertRedirects(self.post(self.admin, 'core:update_text_file', {'file_url': link}), self.detail)
        response = self.post(self.admin, 'core:update_text_file', {'file_url': 'javascript:alert(1)'})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['text_file_form'].errors)
        self.text.refresh_from_db()
        self.assertEqual(self.text.file_url, link)
        response = self.post(self.admin, 'core:update_text_file', {'file_url': link}, text=self.chapter)
        self.assertEqual(response.status_code, 403)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.file_url, '')

    def test_chapter_tags_are_edited_in_novel_view_and_translation_opens_its_page(self):
        response = self.post(self.editor, 'core:update_text_tags', {'tags': 'smoki', 'genre': 'fantasy'},
                             text=self.chapter)
        self.assertRedirects(response, reverse('core:novel_detail', args=[self.novel.pk]), fetch_redirect_response=False)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.tags, '')
        translated = Text.objects.create(title='Przekład', length=1,
                                         anthology=Anthology.objects.create(title='Przekłady', is_translated=True))
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get(reverse('core:assigned_text_detail', args=[translated.pk])),
                             reverse('core:translation_detail', args=[translated.pk]))
