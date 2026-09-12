import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.db import IntegrityError, transaction
from django.http import QueryDict
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.tests import CoreTestDataMixin
from core.selectors.texts import text_detail_context, my_texts_context
from core.selectors.history import historical_team_members
from core.selectors.reports import workflow_activity_context
from core.views.people import _profile_assignments
from people.models import Person
from texts.models import Text, HistoricalTextAssignment
from workflow.models import WorkflowRoleAssignment, WorkflowStage


class HistoricalAssignmentTests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.text = Text.objects.create(title="Archiwalny tekst", length=100, is_historical=True)
        self.text.authors.add(self.author)
        self.person = self.editor.person_profile

    def history(self, **kwargs):
        data = dict(text=self.text, person=self.person, person_name=str(self.person), role="editor", position=2)
        data.update(kwargs)
        return HistoricalTextAssignment.objects.create(**data)

    def test_detail_and_profile_show_additional_roles_without_active_permissions(self):
        self.history()
        self.history(role="verifier", position=4)
        context = text_detail_context(user=self.editor, text=self.text)
        labels = [m["label"] for m in context["team_members"]]
        self.assertIn("Redaktor 2", labels)
        self.assertIn("Weryfikator 4", labels)
        self.assertFalse(context["is_assigned"])
        self.assertFalse(context["can_add_note"])
        self.assertEqual(context["user_assignments"], [])
        assignments, summary = _profile_assignments(self.person, include_authors=False)
        self.assertEqual({r['get_role_display'] for r in assignments}, {"Redaktor 2", "Weryfikator 4"})
        self.assertEqual(summary, dict(active=0, reserved=0, completed=0))
        self.assertTrue(all(not row['text']['authors']['all'] for row in assignments))
        self.client.force_login(self.superuser)
        for url in (reverse('core:assigned_text_detail', args=[self.text.pk]), reverse('core:person_detail', args=[self.person.pk])):
            response = self.client.get(url)
            self.assertContains(response, "Redaktor 2")
            self.assertContains(response, "Weryfikator 4")

    def test_nonhistorical_text_hides_rows_and_rejects_validation(self):
        item = self.history()
        self.text.is_historical = False
        self.text.save(update_fields=['is_historical'])
        self.assertEqual(historical_team_members(self.text), [])
        self.assertEqual(_profile_assignments(self.person, include_authors=False)[0], [])
        item.text = self.text
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_profile_without_user_and_deleted_person_name(self):
        person = Person.objects.create(first_name='Dawna', last_name='Osoba', email='dawna@example.com')
        self.history(person=person, person_name='Dawna Osoba', is_completed=True)
        assignments, summary = _profile_assignments(person, include_authors=False)
        self.assertEqual(len(assignments), 1)
        self.assertEqual(summary['completed'], 1)
        person.delete()
        self.assertEqual(historical_team_members(self.text)[0]['user']['get_full_name'], 'Dawna Osoba')

    def test_current_unique_constraint_remains(self):
        self.history()
        WorkflowRoleAssignment.objects.create(text=self.text, role='editor', assigned_to=self.editor)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkflowRoleAssignment.objects.create(text=self.text, role='editor', assigned_to=self.superuser)

    def test_reports_include_history_without_dates_and_filter_it(self):
        self.history(role='verifier', position=4)
        kwargs = dict(user=self.superuser, stage_roles={'third_verification':'verifier_3'}, people_role_name='Weryfikator', people_context_name='verifiers')
        context = workflow_activity_context(params=QueryDict(), **kwargs)
        rows = [r for r in context['activity_rows'] if r.get('is_historical')]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['role'], 'Weryfikator 4')
        self.assertIsNone(rows[0]['started_at'])
        self.assertFalse(rows[0]['is_completed'])
        self.assertIn(self.person.pk, [p['pk'] for p in context['verifiers']])
        filtered = workflow_activity_context(params=QueryDict('date_from=2020-01-01'), **kwargs)
        self.assertEqual(filtered['activity_rows'], [])

    def test_admin_is_read_only(self):
        item = self.history()
        request = RequestFactory().get('/')
        request.user = self.superuser
        model_admin = admin.site._registry[HistoricalTextAssignment]
        self.assertTrue(model_admin.has_view_permission(request, item))
        for method in ('has_add_permission', 'has_change_permission', 'has_delete_permission'):
            self.assertFalse(getattr(model_admin, method)(request, item))
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.post(reverse('admin:texts_historicaltextassignment_add'), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse('admin:texts_historicaltextassignment_change', args=[item.pk]), {'notes':'zmiana'}).status_code, 403)

    def test_import_preview_commit_repeat_conflict_and_atomic_failure(self):
        self.text.is_historical = False
        self.text.save(update_fields=['is_historical'])
        row = dict(text_id=self.text.pk, person_id=self.person.pk, role='editor', position=2)
        with TemporaryDirectory() as directory:
            path = Path(directory)/'import.json'
            path.write_text(json.dumps([row]))
            call_command('import_historical_assignments', str(path), stdout=StringIO())
            self.text.refresh_from_db()
            self.assertFalse(self.text.is_historical)
            self.assertFalse(HistoricalTextAssignment.objects.exists())
            for _ in range(2):
                call_command('import_historical_assignments', str(path), commit=True, stdout=StringIO())
            self.assertEqual(HistoricalTextAssignment.objects.count(), 1)
            self.text.refresh_from_db()
            self.assertTrue(self.text.is_historical)
            path.write_text(json.dumps([dict(row, notes='nadpisanie')]))
            with self.assertRaises(CommandError):
                call_command('import_historical_assignments', str(path), commit=True, stdout=StringIO())
            path.write_text(json.dumps([dict(row, role='verifier', position=4), dict(row, role='unknown', position=9)]))
            with self.assertRaises(CommandError):
                call_command('import_historical_assignments', str(path), commit=True, stdout=StringIO())
            self.assertEqual(HistoricalTextAssignment.objects.count(), 1)

    def test_import_rejects_duplicate_of_primary_role(self):
        WorkflowRoleAssignment.objects.create(text=self.text, role='editor', assigned_to=self.editor)
        item = HistoricalTextAssignment(text=self.text, person=self.person, person_name=str(self.person), role='editor', position=1)
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_report_pages_render_history_for_each_family(self):
        for role, position in [('editor', 2), ('verifier', 4), ('proofreader', 5)]:
            self.history(role=role, position=position)
        self.client.force_login(self.superuser)
        for route, label in [('editor_activity', 'Redaktor 2'), ('verifier_activity', 'Weryfikator 4'), ('proofreader_activity', 'Korektor 5')]:
            response = self.client.get(reverse('core:' + route))
            self.assertContains(response, label)
            self.assertContains(response, 'Historyczne')

    def test_multiple_people_in_same_numbered_role(self):
        first = self.history(role='proofreader', position=3)
        second = self.history(role='proofreader', position=3, participant=2)
        third = self.history(role='proofreader', position=3, participant=3)
        for item in (first, second, third):
            item.full_clean()
        self.assertEqual(second.role_label, 'Korektor 3 · osoba 2')
        self.assertEqual(third.role_label, 'Korektor 3 · osoba 3')
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())

    def test_extra_participant_can_accompany_primary_numbered_assignment(self):
        WorkflowRoleAssignment.objects.create(text=self.text, role='proofreader_3', assigned_to=self.editor)
        item = self.history(role='proofreader', position=3, participant=2)
        item.full_clean()
        self.assertEqual(item.role_label, 'Korektor 3 · osoba 2')

    def test_source_status_and_completed_history_need_no_dates(self):
        item = self.history(source_row=152, source_status='ready', is_completed=True)
        item.full_clean()
        rows, summary = _profile_assignments(self.person, include_authors=False)
        self.assertEqual(rows[0]['latest_stage']['get_stage_type_display'], 'Gotowe')
        self.assertIsNone(rows[0]['latest_stage']['started_at'])
        self.assertEqual(summary, dict(active=0, reserved=0, completed=1))
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())

    def test_old_stored_dates_never_drive_history_display_or_date_reports(self):
        from datetime import date
        item = self.history(started_at=date(2020, 1, 1), ended_at=date(2020, 2, 1))
        with self.assertRaises(ValidationError):
            item.full_clean()
        rows, _ = _profile_assignments(self.person, include_authors=False)
        self.assertIsNone(rows[0]['latest_stage']['started_at'])
        self.assertIsNone(rows[0]['latest_stage']['ended_at'])
        kwargs = dict(user=self.superuser, stage_roles={'editing':'editor'}, people_role_name='Redaktor', people_context_name='editors')
        unfiltered = workflow_activity_context(params=QueryDict(), **kwargs)['activity_rows']
        self.assertIsNone(unfiltered[0]['started_at'])
        self.assertEqual(workflow_activity_context(params=QueryDict('date_from=2019-01-01'), **kwargs)['activity_rows'], [])

    def test_import_participants_and_reject_fabricated_dates(self):
        row = dict(text_id=self.text.pk, person_id=self.person.pk, role='proofreader', position=3, source_status='ready', source_row=103, is_completed=True)
        with TemporaryDirectory() as directory:
            path = Path(directory)/'history.json'
            path.write_text(json.dumps([dict(row, participant=1), dict(row, participant=2)]))
            for _ in range(2):
                call_command('import_historical_assignments', str(path), commit=True, stdout=StringIO())
            self.assertEqual(HistoricalTextAssignment.objects.count(), 2)
            path.write_text(json.dumps([dict(row, participant=3, started_at='2020-01-01')]))
            with self.assertRaises(CommandError):
                call_command('import_historical_assignments', str(path), commit=True, stdout=StringIO())
            self.assertEqual(HistoricalTextAssignment.objects.count(), 2)

    def test_historical_primary_replaces_empty_team_placeholder(self):
        self.history(position=1)
        context = text_detail_context(user=self.superuser, text=self.text)
        editors = [m for m in context['team_members'] if m['role'] == 'editor']
        self.assertEqual(len(editors), 1)
        self.assertTrue(editors[0]['is_historical'])
