"""Zmiany z 10.10 (druga partia): import uwag do antologii, dostęp do recenzji,
wyniki wyszukiwania, Teksty do wzięcia i konwerter bez opcji miękkich enterów."""
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse

from core.models import AnthologyCorrection
from core.odkurzacz_forms import DocumentConversionForm
from core.views.search import RESULT_LIMIT
from texts.models import Anthology, Review, Text
from workflow.tests import create_member


class CorrectionImportTests(TestCase):
    def setUp(self):
        self.reporter = create_member('import-reporter', 'Redaktor')
        self.reporter.first_name, self.reporter.last_name = 'Anna', 'Zgłaszająca'
        self.reporter.save()
        person = self.reporter.person_profile
        person.first_name, person.last_name = 'Anna', 'Zgłaszająca'
        person.save()
        self.book = Anthology.objects.create(title='Antologia wydana', status=Anthology.Status.READY)
        self.text = Text.objects.create(title='Opowiadanie pierwsze', anthology=self.book, length=10)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def write(self, rows):
        path = Path(self.temp.name) / 'uwagi.json'
        path.write_text(json.dumps({'schema': 'anthology-corrections-v1', 'corrections': rows}, ensure_ascii=False),
                        encoding='utf-8')
        return str(path)

    def run_import(self, path, *extra):
        out = StringIO()
        call_command('import_anthology_corrections', path, *extra, stdout=out)
        return json.loads(out.getvalue())

    def rows(self):
        return [
            {'anthology': 'Antologia wydana', 'reporter': 'Anna Zgłaszająca', 'story': 'Opowiadanie pierwsze',
             'fragment': 'Ala ma kota', 'problem': 'literówka', 'suggestion': 'Ala ma kota.'},
            {'anthology': 'antologia  WYDANA', 'reporter': 'Karolina', 'story': 'Inne miejsce',
             'fragment': 'Paginacja', 'problem': 'zła numeracja', 'suggestion': ''},
            {'anthology': 'Antologia wydana', 'reporter': 'Karolina', 'story': 'Audiodeskrypcja',
             'fragment': 'tytuł w cudzysłowie', 'problem': '', 'suggestion': ''},
        ]

    def test_preview_then_apply_then_repeat(self):
        path = self.write(self.rows())
        self.assertEqual(self.run_import(path)['created'], 3)
        self.assertFalse(AnthologyCorrection.objects.exists())
        report = self.run_import(path, '--apply')
        self.assertEqual(report['created'], 3)
        first = AnthologyCorrection.objects.get(fragment='Ala ma kota')
        self.assertEqual((first.text, first.submitted_by, first.story_title), (self.text, self.reporter, 'Opowiadanie pierwsze'))
        other = AnthologyCorrection.objects.get(fragment='Paginacja')
        self.assertIsNone(other.submitted_by)
        self.assertEqual((other.reporter_name, other.story_title, other.text), ('Karolina', 'Inne miejsce', None))
        self.assertEqual(AnthologyCorrection.objects.get(fragment='tytuł w cudzysłowie').story_title, 'Audiodeskrypcja')
        again = self.run_import(path, '--apply')
        self.assertEqual((again['created'], again['unchanged']), (0, 3))
        self.client.force_login(self.reporter)
        page = self.client.get(reverse('core:anthology_corrections'))
        self.assertContains(page, 'Karolina')

    def test_unknown_or_unpublished_anthology_stops_everything(self):
        Anthology.objects.create(title='W przygotowaniu')
        for title in ('Nie ma takiej', 'W przygotowaniu'):
            rows = self.rows()
            rows[1]['anthology'] = title
            with self.assertRaises(CommandError):
                self.run_import(self.write(rows), '--apply')
        self.assertFalse(AnthologyCorrection.objects.exists())


class ReviewAccessTests(TestCase):
    def setUp(self):
        self.editor = create_member('no-reviewer', 'Redaktor')
        self.reviewer = create_member('with-reviewer', 'Recenzent')
        book = Anthology.objects.create(title='Antologia z recenzją')
        self.text = Text.objects.create(title='Tekst z recenzją', anthology=book, length=10)
        self.review = Review.objects.create(title='Tekst z recenzją', status='accepted', anthology=book,
                                            author_first_name='A', author_last_name='B', email='a@example.test',
                                            length=10, copied_text=self.text)

    def test_member_without_reviewer_role_has_no_review_menu_but_opens_text_review(self):
        self.client.force_login(self.editor)
        home = self.client.get(reverse('core:home'))
        self.assertNotContains(home, 'data-menu-section="submissions"')
        self.assertEqual(self.client.get(reverse('core:review_list')).status_code, 403)
        detail = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertContains(detail, reverse('core:assigned_review_detail', args=[self.review.pk]))
        review = self.client.get(reverse('core:assigned_review_detail', args=[self.review.pk]))
        self.assertEqual(review.status_code, 200)
        self.assertContains(review, 'Wróć do tekstu')

    def test_reviewer_keeps_the_menu(self):
        self.client.force_login(self.reviewer)
        self.assertContains(self.client.get(reverse('core:home')), 'data-menu-section="submissions"')
        self.assertEqual(self.client.get(reverse('core:review_list')).status_code, 200)


class SmallChangesTests(TestCase):
    def test_search_pages_have_fifteen_results(self):
        self.assertEqual(RESULT_LIMIT, 15)

    def test_converter_has_no_soft_whitespace_option(self):
        self.assertNotIn('remove_soft_whitespace', DocumentConversionForm(prefix='convert').fields)

    def test_take_stage_label_is_only_for_screen_readers_in_the_table(self):
        source = Path(__file__).resolve().parent / 'templates' / 'core' / 'includes' / 'take_stage_form.html'
        self.assertIn('{% if compact %}class="visually-hidden"', source.read_text(encoding='utf-8'))
