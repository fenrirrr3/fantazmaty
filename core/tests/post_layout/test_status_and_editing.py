"""Korekta poskładowa: zmiany statusu z datami etapów, edycja wpisu, operacje zbiorcze i miękkie usuwanie."""
from datetime import date
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import PostLayoutAssignment, ProgramPreferences
from core.post_layout import AssignmentEditForm, StaleAssignment, bulk_change, change_status, edit_assignment, selection_token
from fantazmaty.testing import create_member
from texts.models import Anthology

from .support import PostLayoutTestData


class StatusTransitionTests(PostLayoutTestData, TestCase):
    def test_transitions_record_stage_dates_and_reject_stale_skip_and_reverse(self):
        item = self.create()
        old_version = item.version
        self.assertEqual(self.transition(item, 'completed').status_code, 400)
        # Daty po przydziale, by ograniczenie chronologii w bazie miało znaczenie.
        with patch('core.post_layout.timezone.localdate', return_value=item.assigned_start):
            self.assertEqual(self.transition(item, 'in_progress').status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.assigned_end, item.work_start)
        self.assertIsNone(item.work_end)
        self.assertIsNone(item.completed_on)
        self.assertEqual(self.transition(item, 'completed', version=old_version).status_code, 409)
        self.assertEqual(self.transition(item, 'completed').status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.work_end, item.completed_on)
        self.assertEqual(item.status, 'completed')
        # Krok wstecz jest dozwolony i czyści daty wycofanego kroku; dwa kroki – nie.
        self.assertEqual(self.transition(item, 'assigned').status_code, 400)
        self.assertEqual(self.transition(item, 'in_progress').status_code, 302)
        item.refresh_from_db()
        self.assertIsNone(item.completed_on)
        self.assertEqual(self.transition(item, 'completed').status_code, 302)

    def test_overlapping_ranges_revert_clears_dates_and_soft_delete_can_be_restored(self):
        first, second = (PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.reader,
                                                             page_from=start, page_to=end, created_by=self.manager)
                         for start, end in ((1, 20), (10, 30)))
        for status in ('in_progress', 'completed', 'in_progress'):
            first = change_status(user=self.reader, pk=first.pk, status=status, version=first.version)
        self.assertIsNone(first.completed_on)
        self.assertIsNotNone(first.work_start)
        first = change_status(user=self.reader, pk=first.pk, status='assigned', version=first.version)
        self.assertIsNone(first.work_start)
        bulk_change(user=self.manager, selected=[selection_token(self.manager, second)], operation='delete',
                    confirm_delete=True)
        second.refresh_from_db()
        self.assertIsNotNone(second.deleted_at)
        self.assertEqual(PostLayoutAssignment.objects.count(), 2)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.url).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.client.get(self.url, {'show_deleted': '1'}).context['page_obj'].paginator.count, 1)
        bulk_change(user=self.manager, selected=[selection_token(self.manager, second)], operation='restore')
        second.refresh_from_db()
        self.assertIsNone(second.deleted_at)


class AssignmentEditTests(PostLayoutTestData, TestCase):
    def test_edit_completed_dates_and_profile_reassignment(self):
        item = self.completed_item()
        self.client.force_login(self.manager)
        url = reverse('core:post_layout_edit', args=[item.pk])
        self.assertContains(self.client.get(url), 'type="date"')
        self.assertEqual(self.client.post(url, self.edit_data(item)).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.proofreader, self.other)
        self.assertEqual((item.page_from, item.page_to), (10, 30))
        self.assertEqual(item.assigned_end, date(2026, 1, 4))
        self.assertEqual(item.assigned_end, item.work_start)
        self.assertEqual(item.work_end, item.completed_on)
        self.assertEqual(item.completed_on, date(2026, 1, 6))
        self.assertEqual(item.version, 2)
        for user, count in ((self.reader, 0), (self.other, 1)):
            page = self.client.get(reverse('core:person_detail', args=[user.person_profile.pk]))
            self.assertEqual(len(page.context['assignments']), count)

    def test_edit_guards_and_disabled_future_dates(self):
        item = self.item()
        url = reverse('core:post_layout_edit', args=[item.pk])
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.edit_data(item)).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(url, self.edit_data(item, page_to=1)).status_code, 400)
        self.assertEqual(self.client.post(url, self.edit_data(item)).status_code, 302)
        self.assertEqual(self.client.post(url, self.edit_data(item)).status_code, 409)
        item.refresh_from_db()
        # Daty etapów, których przydział jeszcze nie osiągnął, są ignorowane.
        self.assertIsNone(item.work_start)
        self.assertIsNone(item.completed_on)
        self.assertEqual(item.status, 'assigned')
        item.status = 'completed'
        item.assigned_end = item.work_start = date(2026, 1, 4)
        item.work_end = item.completed_on = date(2026, 1, 6)
        item.save()
        self.assertEqual(self.client.post(url, self.edit_data(item, completed_on='2026-01-01')).status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.completed_on, date(2026, 1, 6))


    def test_edit_form_checks_date_order_and_both_ends_of_page_range(self):
        item = self.completed_item()
        for values, field in (({'work_start': '2026-01-01'}, 'work_start'), ({'page_from': ''}, 'page_from'),
                              ({'page_to': ''}, 'page_to'), ({'page_from': 30, 'page_to': 10}, 'page_to')):
            form = AssignmentEditForm(self.edit_data(item, assigned_start='2026-01-02', **values), instance=item)
            self.assertFalse(form.is_valid())
            self.assertIn(field, form.errors)
        with self.assertRaises(PermissionDenied):
            edit_assignment(user=self.reader, pk=item.pk, version=item.version, data=self.edit_data(item))
        item.refresh_from_db()
        self.assertEqual((item.proofreader, item.version), (self.reader, 1))


class BulkOperationTests(PostLayoutTestData, TestCase):
    def test_bulk_reassignment_and_status_change_in_one_transaction(self):
        one = self.item()
        two = self.item(status='in_progress', assigned_end=date(2026, 1, 2), work_start=date(2026, 1, 2))
        self.assertEqual(self.bulk([one, two], 'assign', bulk_proofreader=self.other.pk).status_code, 302)
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual(one.proofreader, self.other)
        self.assertEqual(two.proofreader, self.other)
        # Jeden wpis nie może przeskoczyć kroku – cała operacja jest wycofywana.
        self.assertEqual(self.bulk([one, two], 'status', bulk_status='completed').status_code, 400)
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual((one.status, two.status), ('assigned', 'in_progress'))
        self.assertEqual(self.bulk([one, two], 'status', bulk_status='in_progress').status_code, 302)
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual(self.bulk([one, two], 'status', bulk_status='completed').status_code, 302)
        for item in (one, two):
            item.refresh_from_db()
            self.assertEqual(item.status, 'completed')
            self.assertEqual(item.work_end, item.completed_on)
        one.status = 'assigned'
        one.assigned_end = one.work_start = one.work_end = one.completed_on = None
        one.save()
        # Pierwszy wpis idzie naprzód, ukończony drugi cofa się o krok: oba są pojedynczymi krokami.
        self.assertEqual(self.bulk([one, two], 'status', bulk_status='in_progress').status_code, 302)
        two.refresh_from_db()
        self.assertEqual(two.status, 'in_progress')
        self.assertIsNone(two.completed_on)

    def test_bulk_stale_selection_and_deletion_permissions(self):
        item = self.item()
        stale = selection_token(self.manager, item)
        self.assertEqual(self.bulk([item], 'assign', bulk_proofreader=self.other.pk).status_code, 302)
        self.assertEqual(self.client.post(self.url, {'action': 'bulk', 'operation': 'delete', 'selected': [stale],
                                                     'confirm_delete': '1'}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'action': 'bulk', 'operation': 'assign', 'selected': [stale],
                                                     'bulk_proofreader': self.reader.pk}).status_code, 409)
        item.refresh_from_db()
        self.assertEqual(self.bulk([item], 'delete', user=self.reader, confirm_delete='1').status_code, 403)
        self.assertEqual(self.bulk([item], 'delete', user=self.admin).status_code, 400)
        self.assertTrue(PostLayoutAssignment.objects.filter(pk=item.pk).exists())
        self.assertEqual(self.bulk([item], 'delete', user=self.admin, confirm_delete='1').status_code, 302)
        self.assertFalse(PostLayoutAssignment.objects.present().filter(pk=item.pk).exists())
        self.assertTrue(PostLayoutAssignment.objects.deleted().filter(pk=item.pk).exists())
        profile = self.client.get(reverse('core:person_detail', args=[self.other.person_profile.pk]))
        self.assertFalse(profile.context['assignments'])

    def test_invalid_bulk_requests_change_nothing(self):
        item = self.item()
        deleted = self.item(deleted_at=timezone.now())
        token = selection_token(self.manager, item)
        for kwargs, error in [
                ({'operation': 'archive'}, 'Wybierz operację'),
                ({'operation': 'assign', 'selected': []}, 'Zaznacz od 1 do 500'),
                ({'operation': 'assign', 'selected': [token] * 501}, 'Zaznacz od 1 do 500'),
                ({'operation': 'assign', 'selected': [selection_token(self.admin, item)]}, 'wygasło'),
                ({'operation': 'assign', 'selected': ['zmyślony']}, 'wygasło'),
                ({'operation': 'assign', 'selected': [token, selection_token(self.manager, deleted)],
                  'proofreader': self.other.pk}, 'najpierw przywróć'),
                ({'operation': 'assign', 'proofreader': 'abc'}, 'Wybierz korektora'),
                ({'operation': 'assign', 'proofreader': '１'}, 'Wybierz korektora'),
                ({'operation': 'assign', 'proofreader': self.editor.pk}, 'nie jest aktywnym'),
                ({'operation': 'assign', 'proofreader': '9' * 19}, 'nie jest aktywnym'),
                ({'operation': 'status', 'status': 'lost'}, 'prawidłowy status')]:
            kwargs = {'selected': [token], **kwargs}
            with self.subTest(kwargs={k: v for k, v in kwargs.items() if k != 'selected'}):
                with self.assertRaisesMessage(ValidationError, error):
                    bulk_change(user=self.manager, **kwargs)
        with self.assertRaises(StaleAssignment):
            bulk_change(user=self.manager, selected=['zmyślony'], operation='delete')
        with self.assertRaises(PermissionDenied):
            bulk_change(user=self.reader, selected=[selection_token(self.reader, item)], operation='delete')
        item.refresh_from_db()
        self.assertEqual((item.proofreader, item.status, item.version, item.deleted_at), (self.reader, 'assigned', 1, None))


class PageRangeValidationTests(TestCase):
    def test_page_range_must_be_positive_and_ascending(self):
        """Zakres stron przydziału: od 1 i „do” nie mniejsze niż „od”; etykieta ustawień programów osoby."""
        user = create_member('poskladowa', 'Korektor poskładowy')
        book = Anthology.objects.create(title='Złożona')
        for page_from, page_to in ((0, 3), (5, 3)):
            with self.subTest(pages=(page_from, page_to)):
                item = PostLayoutAssignment(anthology=book, proofreader=user, created_by=user,
                                            page_from=page_from, page_to=page_to)
                with self.assertRaises(ValidationError) as error:
                    item.clean()
                self.assertIn('page_to', error.exception.message_dict)
        self.assertEqual(str(ProgramPreferences(user=user)), 'Ustawienia programów: poskladowa')
