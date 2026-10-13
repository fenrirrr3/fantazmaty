"""Zapisy na stronie tekstu poza tagami: notatki (dodanie, edycja, usunięcie – autor notatki albo
koordynator), notatka koordynatora, ostrzeżenia dotyczące treści i folder Dropbox; uprawnienia osób
przypisanych do bieżącego cyklu, odrzucane dane i rozdziały powieści; oznaczanie nowych notatek
od ostatniej wizyty na karcie tekstu."""
from datetime import timedelta
from tempfile import TemporaryDirectory

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.edit_versions import version_of
from core.models import WorkflowEvent

from fantazmaty.testing import create_member, create_superuser, edit_token
from texts.models import Anthology, Text, TextNote, TextVisit
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


class TextNewNotesTests(TextFieldsTestData, TestCase):
    """Komunikat o nowych notatkach zespołu od ostatniej wizyty na karcie tekstu."""

    def visit(self, user):
        self.client.force_login(user)
        response = self.client.get(self.detail)
        self.assertEqual(response.status_code, 200)
        return response, html.fromstring(response.content)

    def note(self, author, *, age=None, important=False):
        note = TextNote.objects.create(text=self.text, author=author, content='Treść', is_important=important)
        if age is not None:
            TextNote.objects.filter(pk=note.pk).update(created_at=timezone.now() - age)
        return note

    @staticmethod
    def alerts(doc):
        return doc.xpath('//div[contains(@class,"new-notes-alert")]')

    def test_alert_shows_once_and_marks_new_note(self):
        """Notatka innej osoby dodana po wizycie: baner z autorem i kotwicą do notatek oraz etykieta „Nowa”
        przy pierwszym wejściu; kolejne odświeżenie już ich nie pokazuje."""
        self.visit(self.editor)
        self.note(self.coordinator, important=True)
        _, doc = self.visit(self.editor)
        alert = self.alerts(doc)
        self.assertEqual(len(alert), 1)
        text = ' '.join(alert[0].text_content().split())
        self.assertIn('Nowa notatka (ważna) od', text)
        self.assertIn('od fields-coordinator', text)
        self.assertEqual(alert[0].xpath('.//a/@href'), ['#text-notes'])
        self.assertTrue(doc.xpath('//section[@id="text-notes"]'))
        marked = doc.xpath('//details[contains(concat(" ", @class, " "), " is-new ")]')
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0].xpath('.//span[@class="new-note-badge"]/text()'), ['Nowa'])
        _, doc = self.visit(self.editor)
        self.assertFalse(self.alerts(doc))
        self.assertFalse(doc.xpath('//*[contains(@class,"new-note-badge")]'))

    def test_which_notes_count_as_new(self):
        """Nowe są tylko cudze notatki (także z usuniętego konta) nowsze niż poprzednia wizyta;
        bez zapisu wizyty – cudze notatki z ostatnich 14 dni."""
        day = timedelta(days=1)
        cases = [
            # (opis, wiek poprzedniej wizyty albo None, autor, wiek notatki, czy nowa)
            ('cudza po wizycie', 2 * day, 'other', day, True),
            ('z usuniętego konta po wizycie', 2 * day, None, day, True),
            ('własna po wizycie', 2 * day, 'own', day, False),
            ('cudza przed wizytą', day, 'other', 2 * day, False),
            ('pierwsza wizyta – cudza sprzed 13 dni', None, 'other', 13 * day, True),
            ('pierwsza wizyta – cudza sprzed 15 dni', None, 'other', 15 * day, False),
            ('pierwsza wizyta – własna z wczoraj', None, 'own', day, False),
        ]
        authors = {'other': self.coordinator, 'own': self.editor, None: None}
        for label, visit_age, author, note_age, expected in cases:
            with self.subTest(label):
                TextNote.objects.all().delete()
                TextVisit.objects.all().delete()
                if visit_age is not None:
                    TextVisit.objects.create(user=self.editor, text=self.text,
                                             last_seen_at=timezone.now() - visit_age)
                self.note(authors[author], age=note_age)
                _, doc = self.visit(self.editor)
                self.assertEqual(len(self.alerts(doc)), int(expected))
                self.assertEqual(len(doc.xpath('//details[contains(@class,"is-new")]')), int(expected))

    def test_several_new_notes_are_counted_in_one_alert(self):
        """Kilka nowych notatek – jeden baner z liczbą notatek i liczbą ważnych."""
        self.visit(self.editor)
        self.note(self.coordinator)
        self.note(self.coordinator, important=True)
        self.note(self.admin)
        _, doc = self.visit(self.editor)
        self.assertEqual(' '.join(self.alerts(doc)[0].xpath('./strong')[0].text_content().split()),
                         'Nowe notatki: 3')
        self.assertIn('w tym ważne: 1', self.alerts(doc)[0].text_content())

    def test_visit_is_one_row_and_does_not_touch_edit_version_or_logs(self):
        """Wejście zapisuje jeden wiersz wizyty na parę (osoba, tekst) i go aktualizuje,
        nie zmieniając wersji edycji tekstu ani dziennika zdarzeń."""
        self.note(self.coordinator)
        version, events = version_of(self.text), WorkflowEvent.objects.count()
        self.visit(self.editor)
        first = TextVisit.objects.get(user=self.editor, text=self.text).last_seen_at
        self.visit(self.editor)
        visits = TextVisit.objects.filter(user=self.editor, text=self.text)
        self.assertEqual(visits.count(), 1)
        self.assertGreater(visits.get().last_seen_at, first)
        self.assertEqual(str(visits.get()), f'{self.text.pk} – {self.editor.pk}')
        self.assertEqual(version_of(self.text), version)
        self.assertEqual(WorkflowEvent.objects.count(), events)

    def test_visit_is_not_recorded_in_preview_or_after_rejected_form(self):
        """Podgląd jako inna osoba i ponowne wyświetlenie strony po błędzie formularza nie zapisują wizyty."""
        from core.user_preview import SESSION_KEY

        self.client.force_login(self.admin)
        session = self.client.session
        session[SESSION_KEY] = {'actor': self.admin.pk, 'target': self.editor.pk}
        session.save()
        self.assertEqual(self.client.get(self.detail).status_code, 200)
        self.assertFalse(TextVisit.objects.exists())
        self.assertEqual(self.post(self.editor, 'core:add_text_note', {'content': ''}).status_code, 400)
        self.assertFalse(TextVisit.objects.exists())

    def test_team_notes_are_highlighted_and_important_ones_differ(self):
        """Notatki zespołu leżą w karcie z podświetleniem; ważna ma dodatkową klasę i plakietkę."""
        self.note(self.coordinator)
        self.note(self.coordinator, important=True)
        _, doc = self.visit(self.coordinator)
        notes = doc.xpath('//section[contains(@class,"text-notes-card")]//details')
        self.assertEqual(sorted(note.get('class') for note in notes), ['text-note', 'text-note is-important'])
        important = [note for note in notes if 'is-important' in note.get('class')][0]
        self.assertEqual(important.xpath('.//span[@class="important-note-badge"]/text()'), ['Ważna'])
