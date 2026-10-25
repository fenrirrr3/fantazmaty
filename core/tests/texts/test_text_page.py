"""Układ strony tekstu i list tekstów, selektory list (filtry i wyszukiwanie zależne od uprawnień) i szczegółów
tekstu oraz notatki koordynatora (tekstu i ilustracji)."""
from datetime import timedelta
from tempfile import TemporaryDirectory

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from fantazmaty.testing import assign_artists, create_member, create_superuser
from illustrations.editing import edit_token
from illustrations.models import Illustration, Illustrator
from people.models import Person
from texts.models import Anthology, Extract, Review, ReviewAssignment, Text
from workflow.archive_executions import SOURCE
from workflow.import_context import importing_completed
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage, WorkflowStage as S

from .support import source_review


class TextDetailLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('layout57')
        Person.objects.create(user=cls.admin, first_name='Jan', last_name='Test')
        cls.author = Author.objects.create(first_name='Anna', last_name='Test', email='author57@example.test')
        cls.book = Anthology.objects.create(title='Antologia')
        cls.story = Text.objects.create(title='Opowiadanie', anthology=cls.book, length=100)
        cls.story.authors.add(cls.author)
        WorkflowStage.objects.create(text=cls.story, stage_type="ready_for_editing")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_sections_order_forms_and_author_email_with_and_without_note(self):
        for note in ("", "Notatka testowa"):
            self.story.coordinator_note = note
            self.story.save()
            response = self.client.get(reverse("core:assigned_text_detail", args=[self.story.pk]))
            self.assertEqual(response.status_code, 200)
            doc = html.fromstring(response.content)
            self.assertEqual(len(doc.xpath('//*[@id="text-withdraw"]/summary')), 1)
            self.assertFalse(doc.xpath('//*[@id="text-withdraw"]//h3'))
            self.assertEqual(
                doc.xpath('//section[@aria-labelledby="workflow-heading"]//table/@data-pagination'),
                ["off"],
            )
            content = response.content.decode()
            self.assertLess(content.index("Recenzje tekstu"), content.index('id="text-audiobook"'))
            self.assertLess(content.index('id="text-audiobook"'), content.index('id="text-handoff"'))
            self.assertTrue(doc.xpath('//div[@data-text-info-grid]'))
            self.assertFalse(doc.xpath('//div[@data-text-info-grid]/p'))
            self.assertTrue(doc.xpath('//textarea[@name="coordinator_note"]'))
            email = doc.xpath('//a[@href="mailto:author57@example.test"]')[0]
            self.assertEqual(email.xpath('ancestor::dl[1]/div[1]/dt/text()'), ['E-mail autora'])
            for name in ['coordinator_note', 'content_warnings']:
                buttons = doc.xpath(f'//textarea[@name="{name}"]/ancestor::form[1]//button[@type="submit"]')
                self.assertTrue(buttons, name)
                self.assertEqual([' '.join(b.text_content().split()) for b in buttons], ['Zapisz'])

    def test_team_notes_sit_next_to_coordinator_note_and_review_buttons_share_a_row(self):
        """Notatki zespołu i notatka koordynatora (albo przełącznik jej dodania) to dwie kolumny jednej siatki;
        „Zmień lub usuń powiązanie” i „Otwórz zgłoszenie” stoją w jednym kontenerze akcji."""
        Review.objects.create(title='Zgłoszenie układu', anthology=self.book, length=100, copied_text=self.story)
        for note, second in (('', 'details'), ('Notatka testowa', 'section')):
            with self.subTest(note=note):
                self.story.coordinator_note = note
                self.story.save()
                doc = html.fromstring(self.client.get(reverse('core:assigned_text_detail', args=[self.story.pk])).content)
                pair = doc.xpath('//div[@data-text-notes-pair]')[0]
                self.assertEqual(pair.get('class'), 'two-columns text-notes-pair')
                self.assertEqual([child.tag for child in pair], ['section', second])
                self.assertEqual(pair[0].get('id'), 'text-notes')
                self.assertTrue(pair[1].xpath('descendant-or-self::*[@aria-labelledby="coordinator-note-heading"]'))
                actions = doc.xpath('//div[contains(@class,"source-review-actions")]')
                self.assertEqual(len(actions), 1)
                self.assertEqual([' '.join(a.text_content().split()) for a in actions[0].xpath('./a')],
                                 ['Zmień lub usuń powiązanie', 'Otwórz zgłoszenie: Zgłoszenie układu'])
                self.assertIn('actions', actions[0].get('class').split())


class TextListPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('layout-lists')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_anthology_list_shows_texts_without_dropdown(self):
        Anthology.objects.create(title='Antologia v63', status='ready')
        page = self.client.get(reverse('core:anthology_list'))
        self.assertNotContains(page, 'class="anthology-details"')
        self.assertContains(page, 'Teksty antologii')

    def test_extracts_keep_anthology_column_without_group_rows_or_add_button(self):
        author = Author.objects.create(first_name='Anna', last_name='Test')
        Extract.objects.create(author=author, full_name='Anna Test', email='test@example.test',
                               title='Fragment', recruitment='Ekstrakty 1')
        response = self.client.get(reverse('core:extract_list'))
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        self.assertFalse(doc.xpath('//tr[@class="extract-group"]'))
        self.assertNotContains(response, 'Dodaj ekstrakt')
        self.assertTrue(doc.xpath('//thead//th[normalize-space()="Antologia"]'))
        self.assertTrue(doc.xpath('//tbody//td[normalize-space()="Ekstrakty 1"]'))


class CoordinatorNotesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin16')
        cls.artist = create_member('artist16', 'Ilustrator')
        cls.artist_contact = Illustrator.objects.create(first_name='Artysta', last_name='Kontakt')
        cls.book = Anthology.objects.create(title='Nowa antologia', has_illustrations=True)
        cls.text = Text.objects.create(title='Nowy tekst', anthology=cls.book, length=20)
        cls.ill = Illustration.objects.get(text=cls.text)

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=tmp.name)
        settings.enable()
        self.addCleanup(settings.disable)

    def test_text_note_timestamp_is_set_kept_on_other_edits_and_cleared(self):
        self.text.coordinator_note = "Notatka"
        self.text.save(update_fields=["coordinator_note"])
        self.text.refresh_from_db()
        stamp = self.text.coordinator_note_updated_at
        self.assertIsNotNone(stamp)
        self.text.title = "Nowy"
        self.text.save()
        self.text.refresh_from_db()
        self.assertEqual(self.text.coordinator_note_updated_at, stamp)
        self.text.coordinator_note = ""
        self.text.save(update_fields=["coordinator_note"])
        self.text.refresh_from_db()
        self.assertIsNone(self.text.coordinator_note_updated_at)

    def test_illustration_notes_permissions_conflict_and_search_markup(self):
        self.client.force_login(self.admin)
        url = reverse("illustrations:illustration_detail", args=[self.ill.pk])
        old = edit_token(self.admin, self.ill)
        response = self.client.post(url, {
            "action": "coordinator_notes", "version": old, "coordinator_notes": "Proszę o ciemne tło.",
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            url, {"action": "coordinator_notes", "version": old, "coordinator_notes": "stary zapis"}
        )
        self.assertEqual(response.status_code, 409)
        response = self.client.get(url)
        self.assertContains(response, 'id="illustrator-search"')
        self.assertRegex(response.content.decode(), r"illustrator_search(\.[0-9a-f]+)?\.js")
        self.ill.refresh_from_db()
        assign_artists(self.ill, [self.artist_contact], status="assigned")
        self.client.force_login(self.artist)
        response = self.client.post(url, {
            "action": "coordinator_notes",
            "version": edit_token(self.artist, self.ill),
            "coordinator_notes": "nie wolno",
        })
        self.assertEqual(response.status_code, 403)
        self.ill.refresh_from_db()
        self.assertEqual(self.ill.coordinator_notes, "Proszę o ciemne tło.")


class ListSelectorTests(TestCase):
    """Listy tekstów i prac: filtry i wyszukiwanie zależne od uprawnień."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.admin = create_superuser('listy-admin')
        cls.member = create_member('listy-redaktor', 'Redaktor')
        cls.coordinator = create_member('listy-koordynator', 'Koordynator redakcji')
        cls.book = Anthology.objects.create(title='Antologia')
        cls.author = Author.objects.create(first_name='Ewa', last_name='Wrzosowska', email='ewa@example.test')
        cls.waiting = Text.objects.create(title='Mgła', anthology=cls.book, length=100)
        cls.waiting.authors.add(cls.author)
        S.objects.create(text=cls.waiting, stage_type='ready_for_editing')
        cls.fresh = Text.objects.create(title='Mgławica', anthology=cls.book, length=100)

    def titles(self, user, route, params, key='texts'):
        self.client.force_login(user)
        response = self.client.get(reverse(route), params)
        self.assertEqual(response.status_code, 200)
        return response, response.context[key]

    def test_status_filter_none_lists_texts_without_stage(self):
        _, texts = self.titles(self.member, 'core:text_list', {'status': 'none'})
        self.assertEqual([row['title'] for row in texts], ['Mgławica'])

    def test_author_search_requires_author_data_permission(self):
        """Członek zespołu i koordynator szukają tylko po tytule; superuser także po autorze."""
        cases = (
            (self.member, 'core:text_list', 'texts', 'Wrzosowska', []),
            (self.member, 'core:text_list', 'texts', 'Mgła', ['Mgła', 'Mgławica']),
            (self.coordinator, 'core:workflow_list', 'stages', 'Wrzosowska', []),
            (self.coordinator, 'core:workflow_list', 'stages', 'Mgławica', ['Mgławica']),
            (self.admin, 'core:available_texts', 'available_stages', 'Wrzosowska', ['Mgła']),
        )
        for user, route, key, query, expected in cases:
            with self.subTest(user=user.username, route=route, query=query):
                _, rows = self.titles(user, route, {'q': query}, key)
                titles = [row['title'] if 'title' in row else row['text']['title'] for row in rows]
                self.assertEqual(sorted(titles), expected)

    def test_unknown_my_texts_view_falls_back_to_active(self):
        response, _ = self.titles(self.member, 'core:my_texts', {'view': 'kosz'})
        self.assertEqual(response.context['selected_view'], 'active')

    def test_workflow_table_marks_future_booking_as_reserved(self):
        editor = A.objects.create(text=self.fresh, role='editor', assigned_to=self.member)
        S.objects.create(text=self.fresh, stage_type='editing', assignment=editor,
                         started_at=self.today + timedelta(days=3))
        _, rows = self.titles(self.coordinator, 'core:workflow_list', {'q': 'Mgławica'}, 'stages')
        row, = rows
        editor_cell = next(cell for cell in row['role_cells'] if cell['role'] == 'editor')
        entry, = editor_cell['entries']
        self.assertEqual((entry['state'], entry['tone']), ('Zarezerwowano', 'pending'))
        self.assertEqual(entry['user']['pk'], self.member.pk)


class DetailSelectorTests(TestCase):
    """Strona tekstu: historia dla koordynatora, kontrola redaktora, stylowanie i opinie recenzji źródłowej."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.admin = create_superuser('detal-admin')
        cls.editor = create_member('detal-redaktor', 'Redaktor')
        cls.coordinator = create_member('detal-koordynator', 'Koordynator redakcji')
        cls.reviewer = create_member('detal-recenzent', 'Recenzent')
        cls.book = Anthology.objects.create(title='Antologia')

    def setUp(self):
        self.text = Text.objects.create(title='Szczegóły', anthology=self.book, length=100)

    def detail(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertEqual(response.status_code, 200)
        return response.context

    def done(self, kind, **extra):
        return S.objects.create(text=self.text, stage_type=kind, started_at=self.today,
                                ended_at=self.today, is_completed=True, **extra)

    def test_editor_control_and_styling_rows_follow_their_owners(self):
        editor = A.objects.create(text=self.text, role='editor', assigned_to=self.editor)
        control = S.objects.create(text=self.text, stage_type='editor_control', assignment=editor)
        styling_role = A.objects.create(text=self.text, role='styling', assigned_to=self.admin)
        styling = S.objects.create(text=self.text, stage_type='styling', assignment=styling_role)
        cases = (
            (self.editor, {control.pk: True, styling.pk: False}),
            (self.coordinator, {control.pk: False, styling.pk: False}),
            (self.admin, {control.pk: False, styling.pk: True}),
        )
        for user, expected in cases:
            with self.subTest(user=user.username):
                rows = {row['pk']: row['can_start'] for row in self.detail(user)['stages']}
                self.assertEqual({pk: rows[pk] for pk in expected}, expected)

    def test_source_review_opinions_are_listed(self):
        review = source_review(self.text)
        ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1, opinion='yes')
        ReviewAssignment.objects.create(review=review, user=None, position=2)
        opinions = self.detail(self.admin)['source_review_opinions']
        self.assertEqual([(item['position'], item['reviewer_name']) for item in opinions],
                         [(1, 'detal-recenzent Testowy'), (2, 'Usunięte konto')])
        self.assertEqual(opinions[0]['opinion_value'], 'yes')

    def test_finished_archive_text_lists_performers_of_imported_work(self):
        """Gotowy tekst z tabeli archiwalnej: zespół to wykonawcy zaimportowanej pracy, w kolejności ról."""
        Text.objects.filter(pk=self.text.pk).update(import_source=SOURCE, import_source_row=2)
        token = importing_completed.set(True)
        try:
            for role, kind, user in (('verifier_1', 'first_verification', self.reviewer),
                                     ('editor', 'editing', self.editor)):
                assignment = A.objects.create(text=self.text, role=role, assigned_to=user, is_current=False)
                S.objects.create(text=self.text, stage_type=kind, assignment=assignment, is_completed=True,
                                 imported_completed=True, is_current=False, is_released=False)
        finally:
            importing_completed.reset(token)
        self.done('ready')
        team = self.detail(self.admin)['team_members']
        self.assertEqual([(member['role'], member['user']['pk']) for member in team],
                         [('editor', self.editor.pk), ('verifier_1', self.reviewer.pk)])
        self.assertTrue(all(member['archive_execution'] for member in team))
