"""Słownik zamian Odkurzacza w CMS: dane początkowe, uprawnienia, walidacja i użycie w zadaniach."""
import importlib
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, tag
from django.urls import reverse
from docx import Document

from core.models import ReplacementEntry
from core.odkurzacz_forms import ReplacementEntryForm
from core.program_test_support import run_program
from core.services import program_jobs as jobs
from core.services.odkurzacz import DEFAULT_REPLACEMENTS
from fantazmaty.testing import create_member, create_superuser

from .support import IsolatedProgramDirsMixin, downloaded_document, saved

INITIAL = importlib.import_module('core.migrations.0047_initial_replacements').INITIAL_REPLACEMENTS
LIST_URL = reverse('core:replacement_list')


def entry_data(**values):
    return {'kind': 'word', 'source': 'kotek', 'target': 'kocik', 'is_active': 'on', 'position': '5', 'note': '', **values}


class InitialDictionaryTests(TestCase):
    def test_migration_fills_the_dictionary_with_the_built_in_list(self):
        entries = list(ReplacementEntry.objects.order_by('position', 'pk'))
        self.assertEqual([entry.as_replacement() for entry in entries],
                         [{'kind': kind, 'source': source, 'target': target} for kind, source, target in INITIAL])
        self.assertTrue(all(entry.is_active for entry in entries))
        self.assertEqual(ReplacementEntry.active_replacements(), [entry.as_replacement() for entry in entries])

    def test_migration_copy_matches_the_cleaner_default_list(self):
        """Kopia w migracji odpowiada DEFAULT_REPLACEMENTS z chwili utworzenia słownika."""
        self.assertEqual([{'kind': kind, 'source': source, 'target': target} for kind, source, target in INITIAL],
                         [dict(item) for item in DEFAULT_REPLACEMENTS])

    def test_initial_entries_pass_the_edit_validation(self):
        """Superuser może zapisać każdą pozycję początkową bez poprawiania jej."""
        for entry in ReplacementEntry.objects.all():
            with self.subTest(source=entry.source):
                form = ReplacementEntryForm(entry_data(kind=entry.kind, source=entry.source, target=entry.target),
                                            instance=entry, allow_patterns=True)
                self.assertTrue(form.is_valid(), form.errors)


class DictionaryPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('slownik-redaktor', 'Redaktor')
        cls.coordinator = create_member('slownik-koordynator', 'Koordynator')
        cls.admin = create_superuser('slownik-admin')
        cls.pattern = ReplacementEntry.objects.filter(kind='pattern').first()
        cls.word = ReplacementEntry.objects.filter(kind='word').first()

    def test_team_member_sees_the_list_but_cannot_change_it(self):
        self.client.force_login(self.member)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'niechcąco')
        self.assertContains(response, 'Słownik mogą zmieniać koordynatorzy i superuser.')
        self.assertIsNone(response.context['form'])
        self.assertNotContains(response, reverse('core:replacement_edit', args=[self.word.pk]))
        count = ReplacementEntry.objects.count()
        self.assertEqual(self.client.post(LIST_URL, entry_data()).status_code, 403)
        url = reverse('core:replacement_edit', args=[self.word.pk])
        for data in ({}, {'action': 'toggle'}, {'action': 'delete'}):
            self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(ReplacementEntry.objects.count(), count)

    def test_anonymous_user_is_sent_to_login(self):
        self.assertEqual(self.client.get(LIST_URL).status_code, 302)

    def test_coordinator_adds_edits_disables_and_deletes_words(self):
        self.client.force_login(self.coordinator)
        response = self.client.post(LIST_URL, entry_data(note='Zdrobnienie'))
        self.assertRedirects(response, LIST_URL + '#replacement-table', fetch_redirect_response=False)
        entry = ReplacementEntry.objects.get(source='kotek')
        self.assertEqual((entry.target, entry.note, entry.position, entry.is_active), ('kocik', 'Zdrobnienie', 5, True))
        url = reverse('core:replacement_edit', args=[entry.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {**entry_data(target='kicia'), 'action': 'save'})
        entry.refresh_from_db()
        self.assertEqual(entry.target, 'kicia')
        self.client.post(url, {'action': 'toggle'})
        entry.refresh_from_db()
        self.assertFalse(entry.is_active)
        self.assertNotIn(entry.as_replacement(), ReplacementEntry.active_replacements())
        self.client.post(url, {'action': 'toggle'})
        entry.refresh_from_db()
        self.assertTrue(entry.is_active)
        self.assertEqual(self.client.post(url, {'action': 'nieznana'}).status_code, 400)
        self.client.post(url, {'action': 'delete'})
        self.assertFalse(ReplacementEntry.objects.filter(pk=entry.pk).exists())
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_new_entry_suggests_the_next_position(self):
        self.client.force_login(self.coordinator)
        highest = max(ReplacementEntry.objects.values_list('position', flat=True))
        self.assertEqual(self.client.get(LIST_URL).context['form'].initial['position'], highest + 10)

    def test_only_superuser_adds_or_changes_regular_expressions(self):
        self.client.force_login(self.coordinator)
        response = self.client.get(LIST_URL)
        self.assertEqual([value for value, _ in response.context['form'].fields['kind'].choices], ['word'])
        self.assertContains(response, 'Tylko superuser')
        response = self.client.post(LIST_URL, entry_data(kind='pattern', source=r'\bkot(?P<e>a|y)?\b', target=r'pies\g<e>'))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ReplacementEntry.objects.filter(source__contains='kot(?P').exists())
        url = reverse('core:replacement_edit', args=[self.pattern.pk])
        for data in ({}, {'action': 'toggle'}, {'action': 'delete'}):
            self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 403)
        # Koordynator nie zmieni też słowa we wzorzec.
        word_url = reverse('core:replacement_edit', args=[self.word.pk])
        response = self.client.post(word_url, {**entry_data(kind='pattern', source='abc+', target='x'), 'action': 'save'})
        self.assertEqual(response.status_code, 400)
        self.word.refresh_from_db()
        self.assertEqual(self.word.kind, 'word')

        self.client.force_login(self.admin)
        response = self.client.post(LIST_URL, entry_data(kind='pattern', source=r'\bkot(?P<e>a|y)?\b', target=r'pies\g<e>'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ReplacementEntry.objects.filter(kind='pattern', target=r'pies\g<e>').exists())
        self.client.post(url, {'action': 'toggle'})
        self.pattern.refresh_from_db()
        self.assertFalse(self.pattern.is_active)

    def test_programs_page_links_to_the_dictionary_with_the_active_count(self):
        self.client.force_login(self.member)
        ReplacementEntry.objects.filter(pk=self.word.pk).update(is_active=False)
        active = ReplacementEntry.objects.filter(is_active=True).count()
        response = self.client.get(reverse('core:programs'))
        self.assertContains(response, f'Własne zamiany słownikowe – słownik zamian (aktywne pozycje: {active})', count=2)
        self.assertContains(response, f'href="{LIST_URL}"')
        self.assertNotContains(response, '23 pozycje')
        self.assertContains(self.client.get(LIST_URL), f'Aktywne pozycje ({active})')

    def test_dictionary_pages_highlight_programs_in_navigation(self):
        self.client.force_login(self.coordinator)
        for url in (LIST_URL, reverse('core:replacement_edit', args=[self.word.pk])):
            self.assertEqual(self.client.get(url).context['navigation_current'], 'programs')


class EntryValidationTests(TestCase):
    def form(self, allow_patterns=True, instance=None, **values):
        return ReplacementEntryForm(entry_data(**values), allow_patterns=allow_patterns, instance=instance)

    def assertInvalid(self, form, field, message):
        self.assertFalse(form.is_valid())
        self.assertIn(message, ' '.join(form.errors.get(field, [])), form.errors)

    def test_word_entries(self):
        self.assertTrue(self.form(source='  pół-żartem  ', target='półżartem').is_valid())
        form = self.form(source='  kotek  ')
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['source'], 'kotek')
        self.assertInvalid(self.form(source='kot.*'), 'source', 'Słowo może zawierać litery')
        self.assertInvalid(self.form(source='123'), 'source', 'Słowo może zawierać litery')
        self.assertInvalid(self.form(source='a' * 101), 'source', 'najwyżej 100 znaków')
        self.assertInvalid(self.form(target='   '), 'target', 'To pole jest wymagane.')
        self.assertInvalid(self.form(target='a\tb'), 'target', 'znaków sterujących')
        self.assertInvalid(self.form(target='x' * 201), 'target', 'co najwyżej 200')

    def test_duplicates_are_found_regardless_of_letter_case(self):
        """MySQL porównuje bez wielkości liter – duplikaty sprawdzamy w Pythonie, nie unikalnym indeksem."""
        self.assertInvalid(self.form(source='NIECHCĄCO'), 'source', 'Taka pozycja już jest w słowniku.')
        existing = ReplacementEntry.objects.get(source='niechcąco')
        self.assertTrue(self.form(source='Niechcąco', target='niechcący', instance=existing).is_valid())
        # Ten sam zapis jako wzorzec to inna pozycja.
        self.assertTrue(self.form(kind='pattern', source='niechcąco', target='niechcący').is_valid())

    def test_regular_expressions(self):
        self.assertTrue(self.form(kind='pattern', source=r'\bkot(?P<e>a|y)?\b', target=r'pies\g<e>').is_valid())
        self.assertInvalid(self.form(kind='pattern', source='kot(', target='pies'), 'source', 'Nieprawidłowe wyrażenie')
        self.assertInvalid(self.form(kind='pattern', source=r'\bkot\b', target=r'pies\g<e>'), 'target', 'Nieprawidłowa zamiana')
        self.assertInvalid(self.form(kind='pattern', source=r'\bkot\b', target=r'pies\q'), 'target', 'Nieprawidłowa zamiana')
        self.assertInvalid(self.form(kind='pattern', source=r'\bkot\b', target=r'pies\2'), 'target', 'Nieprawidłowa zamiana')
        for empty in ('x*', r'\b', '(?:)', 'a?'):
            with self.subTest(source=empty):
                self.assertInvalid(self.form(kind='pattern', source=empty, target='y'), 'source', 'pustego tekstu')
        self.assertInvalid(self.form(kind='pattern', source='a' * 501, target='b'), 'source', 'co najwyżej 500')

    def test_patterns_require_permission(self):
        form = self.form(allow_patterns=False, kind='pattern', source=r'\bkot\b', target='pies')
        self.assertFalse(form.is_valid())
        self.assertIn('kind', form.errors)


class DictionaryInCleanerTests(IsolatedProgramDirsMixin, TestCase):
    """Prawdziwy Odkurzacz używa pozycji ze słownika w CMS."""

    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('slownik-odkurzacz', 'Redaktor')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.member)

    def clean(self, text):
        doc = Document()
        doc.add_paragraph(text)
        response = run_program(self.client, {'document': SimpleUploadedFile('tekst.docx', saved(doc).getvalue()),
                                             'rules': ['user_word_corrections']})
        self.assertEqual(response.status_code, 200, getattr(response, 'content', b'')[:300])
        return downloaded_document(response).paragraphs[0].text

    @tag('slow')
    def test_entries_are_applied_disabled_ones_are_not(self):
        ReplacementEntry.objects.create(kind='word', source='kotek', target='kocik', position=1)
        ReplacementEntry.objects.filter(source='zapewnie').update(is_active=False)
        self.assertEqual(self.clean('Niechcąco zapewnie Kotek pickupem.'), 'Niechcący zapewnie Kocik pikapem.')

    @tag('slow')
    def test_empty_dictionary_changes_nothing(self):
        ReplacementEntry.objects.all().delete()
        self.assertEqual(self.clean('Niechcąco pickupem.'), 'Niechcąco pickupem.')

    def test_replacements_are_read_when_the_job_is_created(self):
        with patch.object(jobs, 'launch'):
            response = self.client.post(reverse('core:programs'), {
                'document': SimpleUploadedFile('tekst.docx', saved(Document()).getvalue())}, HTTP_X_PROGRAM_JOB='1')
        self.assertEqual(response.status_code, 202)
        folder = next(jobs.root().iterdir())
        self.assertEqual(len(jobs.read_json(folder / 'request.json')['options']['replacements']), len(INITIAL))
