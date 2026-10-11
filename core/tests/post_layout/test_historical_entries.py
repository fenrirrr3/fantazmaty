"""Historyczne korekty poskładowe (ze stopek, bez dat i stron) oraz zasady dat i zakresu stron wobec wpisów bieżących."""
from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from core.models import PostLayoutAssignment
from core.post_layout import AssignmentEditForm, change_status
from core.selectors.post_layout import profile_post_layout_assignments

from .support import PostLayoutTestData

EMPTY_EDIT = {'page_from': '', 'page_to': '', 'assigned_start': '', 'work_start': '', 'completed_on': ''}


class HistoricalEntryTests(PostLayoutTestData, TestCase):
    def historical(self):
        """Wpis historyczny: zawsze zakończony, bez dat, stron i osoby przypisującej."""
        return PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.reader, historical=True,
                                                   status='completed', assigned_start=None)

    def edit_form(self, item, **values):
        return AssignmentEditForm({'proofreader': self.reader.pk, **EMPTY_EDIT, **values}, instance=item)

    def test_entry_without_dates_in_edit_form_admin_profile_and_list(self):
        item = self.historical()
        form = self.edit_form(item)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        row = profile_post_layout_assignments(self.reader.person_profile)[0]
        self.assertIsNone(row['assigned_at'])
        self.assertNotIn('None', row['text']['title'])
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.url), 'brak zakresu stron')
        page = self.client.get(reverse('admin:core_postlayoutassignment_change', args=[item.pk]))
        self.assertEqual(page.status_code, 200)
        page = self.client.get(reverse('core:person_detail', args=[self.reader.person_profile.pk]))
        self.assertContains(page, 'brak zakresu stron')

    def test_status_of_historical_entry_never_changes(self):
        item = self.historical()
        self.assertIsNone(item.previous_status)
        for status in ('in_progress', 'assigned'):
            with self.assertRaisesMessage(ValidationError, 'Historyczny wpis'):
                change_status(user=self.manager, pk=item.pk, status=status, version=item.version)
        self.assertEqual(self.transition(item, 'in_progress', user=self.manager).status_code, 400)
        item.refresh_from_db()
        self.assertEqual((item.status, item.version), ('completed', 1))

    def test_partial_pages_rejected_but_known_dates_allowed_in_order(self):
        """Wpis historyczny przyjmuje część dat (formularz z panelu admina), ale znane daty muszą być po kolei."""
        item = self.historical()
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
        item = PostLayoutAssignment(anthology=self.book, proofreader=self.reader, status='completed',
                                    assigned_start=None)
        with self.assertRaises(ValidationError):
            item.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            item.save()
        item.historical = True
        item.status = 'assigned'
        with self.assertRaises(ValidationError):
            item.full_clean()
        current = PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.reader,
                                                      created_by=self.reader, page_from=1, page_to=2)
        self.assertFalse(self.edit_form(current, page_from=1, page_to=2).is_valid())
