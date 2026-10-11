"""Import historycznych korekt poskładowych i zasady dat oraz zakresu stron dla wpisów historycznych i bieżących."""
import io
import json
import tempfile
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from core.models import PostLayoutAssignment
from core.post_layout import AssignmentEditForm
from core.selectors.post_layout import profile_post_layout_assignments
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology

EMPTY_EDIT = {'page_from': '', 'page_to': '', 'assigned_start': '', 'work_start': '', 'completed_on': ''}


class HistoricalPostLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Antologia próbna')
        cls.user = create_member('historicalreader', 'Korektor poskładowy')
        person = cls.user.person_profile
        person.first_name, person.last_name = 'Jan', 'Kowalski'
        person.save()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.rows = [{'anthology': self.book.title, 'proofreader': 'Jan Kowalski'}]

    def run_import(self, apply=False, **options):
        (self.path / 'data.json').write_text(
            json.dumps({'schema': 'post-layout-credits-v1', 'credits': self.rows}), encoding='utf-8')
        call_command('import_post_layout_credits', str(self.path / 'data.json'), apply=apply,
                     report=str(self.path / 'report.json'), stdout=io.StringIO(), **options)
        return json.loads((self.path / 'report.json').read_text(encoding='utf-8'))

    def imported(self):
        self.run_import(True)
        return PostLayoutAssignment.objects.get()

    def edit_form(self, item, **values):
        return AssignmentEditForm({'proofreader': self.user.pk, **EMPTY_EDIT, **values}, instance=item)

    def test_preview_apply_repeat_and_unknown_dates(self):
        self.assertEqual(self.run_import()['created'], 1)
        self.assertFalse(PostLayoutAssignment.objects.exists())
        item = self.imported()
        self.assertTrue(item.historical)
        self.assertEqual(item.status, 'completed')
        for field in ('page_from', 'page_to', 'assigned_start', 'assigned_end', 'work_start', 'work_end',
                      'completed_on', 'created_by'):
            self.assertIsNone(getattr(item, field))
        self.assertEqual(self.run_import(True)['unchanged'], 1)
        self.assertEqual(PostLayoutAssignment.objects.count(), 1)

    def test_unknown_person_aborts_all(self):
        self.rows.append({'anthology': self.book.title, 'proofreader': 'Nieznana Osoba'})
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.assertFalse(PostLayoutAssignment.objects.exists())
        report = json.loads((self.path / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(len(report['conflicts']), 1)

    def test_ambiguous_user_aborts(self):
        get_user_model().objects.create_user('duplicate', first_name='Jan', last_name='Kowalski')
        with self.assertRaises(CommandError):
            self.run_import(True)
        self.assertFalse(PostLayoutAssignment.objects.exists())

    def test_existing_work_not_overwritten(self):
        existing = PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.user, page_from=1,
                                                       page_to=20, created_by=self.user)
        with self.assertRaises(CommandError):
            self.run_import(True)
        existing.refresh_from_db()
        self.assertEqual(existing.status, 'assigned')
        self.assertEqual(PostLayoutAssignment.objects.count(), 1)

    def test_explicit_mapping(self):
        self.rows[0]['proofreader'] = 'Dawne nazwisko'
        (self.path / 'map.json').write_text(json.dumps({'users': {'Dawne nazwisko': self.user.pk}}), encoding='utf-8')
        self.run_import(True, mapping=str(self.path / 'map.json'))
        self.assertEqual(PostLayoutAssignment.objects.get().proofreader, self.user)

    def test_imported_entry_in_forms_admin_profile_and_list(self):
        item = self.imported()
        form = self.edit_form(item)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        row = profile_post_layout_assignments(self.user.person_profile)[0]
        self.assertIsNone(row['assigned_at'])
        self.assertNotIn('None', row['text']['title'])
        self.client.force_login(create_superuser('historyadmin'))
        self.assertContains(self.client.get(reverse('core:post_layout')), 'brak zakresu stron')
        page = self.client.get(reverse('admin:core_postlayoutassignment_change', args=[item.pk]))
        self.assertEqual(page.status_code, 200)
        page = self.client.get(reverse('core:person_detail', args=[self.user.person_profile.pk]))
        self.assertContains(page, 'brak zakresu stron')

    def test_partial_pages_rejected_but_known_dates_allowed_in_order(self):
        """Wpis historyczny przyjmuje część dat (formularz z panelu admina), ale znane daty muszą być po kolei."""
        item = self.imported()
        item.page_from = 1
        with self.assertRaises(ValidationError):
            item.full_clean()
        form = self.edit_form(item, completed_on='2026-01-01')
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        saved.full_clean()
        self.assertIsNone(saved.assigned_start)
        self.assertIsNone(saved.work_start)
        self.assertEqual(saved.completed_on, date(2026, 1, 1))
        # Krańce muszą być uporządkowane nawet przy brakującej dacie środkowej.
        self.assertFalse(self.edit_form(saved, completed_on='2026-01-01', assigned_start='2026-02-01').is_valid())
        with self.assertRaises(IntegrityError), transaction.atomic():
            PostLayoutAssignment.objects.filter(pk=saved.pk).update(assigned_start=date(2026, 2, 1))

    def test_normal_work_still_requires_pages_dates_and_assigner(self):
        item = PostLayoutAssignment(anthology=self.book, proofreader=self.user, status='completed',
                                    assigned_start=None)
        with self.assertRaises(ValidationError):
            item.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            item.save()
        item.historical = True
        item.status = 'assigned'
        with self.assertRaises(ValidationError):
            item.full_clean()
        current = PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.user,
                                                      created_by=self.user, page_from=1, page_to=2)
        self.assertFalse(self.edit_form(current, page_from=1, page_to=2).is_valid())
