import uuid
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin
from core.models import AnthologyCorrection, Recruitment
from core.selectors.texts import text_list_context
from texts.models import Text, Extract, Review, Anthology
from workflow.models import WorkflowStage, WorkflowRoleAssignment


class Patch7Tests(CoreTestDataMixin, TestCase):
    def test_other_place_is_saved_and_idempotent(self):
        self.client.force_login(self.reviewer)
        data = dict(anthology=self.anthology.pk, text='', fragment='Spis treści', problem='Literówka', suggestion='Popraw', submission_token=str(uuid.uuid4()))
        for _ in range(2):
            response = self.client.post(reverse('core:anthology_corrections'), data)
            self.assertEqual(response.status_code, 302)
        item = AnthologyCorrection.objects.get()
        self.assertIsNone(item.text_id)
        self.assertEqual(item.story_title, 'Inne miejsce')

    def test_statuses_are_or_other_filters_are_and_and_options_cascade(self):
        other = Anthology.objects.create(title='Druga')
        for anthology, title, status in [(self.anthology, 'Dragon A', 'editing'), (self.anthology, 'Dragon B', 'first_verification'), (other, 'Dragon C', 'editing'), (self.anthology, 'Castle', 'styling')]:
            text = Text.objects.create(anthology=anthology, title=title, length=1)
            WorkflowStage.objects.create(text=text, stage_type=status)
        params = QueryDict(f'anthology={self.anthology.pk}&status=editing&status=first_verification&q=DRAGON')
        context = text_list_context(user=self.coordinator, params=params)
        self.assertEqual({row['title'] for row in context['texts']}, {'Dragon A', 'Dragon B'})
        self.assertNotIn('styling', dict(context['status_choices']))

    def test_extract_permissions_and_workflow_isolation(self):
        for user in (self.reviewer, self.editor):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('core:extract_list')).status_code, 403)
            self.assertEqual(self.client.post(reverse('core:extract_add'), {}).status_code, 403)
        self.client.force_login(self.coordinator)
        response = self.client.post(reverse('core:extract_add'), {'author':self.author.pk, 'title':'Secret extract', 'submission_dates':'2026-09-11', 'recruitment':'Jesień', 'accepted_titles':'Secret extract'})
        self.assertEqual(response.status_code, 302)
        item = Extract.objects.get()
        self.assertEqual(item.email, self.author.email)
        self.assertEqual(item.full_name, str(self.author))
        self.assertEqual(Text.objects.count(), 0)
        self.assertEqual(WorkflowStage.objects.count(), 0)
        self.assertContains(self.client.get(reverse('core:extract_list')), 'Secret extract')
        self.assertNotContains(self.client.get(reverse('core:text_list')), 'Secret extract')
        response = self.client.get(reverse('core:global_search'), {'query':'Secret extract'})
        main = response.content.decode().split('<main', 1)[1]
        self.assertNotIn('href="/ekstrakty/', main)

    def test_recruitment_department_and_conflict(self):
        self.client.force_login(self.coordinator)
        data = {'first_name':'Anna', 'last_name':'Test', 'email':'ANNA@example.com', 'department':'invalid', 'submitted_at':timezone.localdate().isoformat(), 'status':'new'}
        self.assertEqual(self.client.post(reverse('core:recruitment_add'), data).status_code, 400)
        data['department'] = 'editors'
        self.assertEqual(self.client.post(reverse('core:recruitment_add'), data).status_code, 302)
        item = Recruitment.objects.get()
        self.assertEqual(item.email, 'anna@example.com')
        self.assertEqual(item.full_name, 'Anna Test')
        response = self.client.post(reverse('core:recruitment_edit', args=[item.pk]), {**data, 'version':'stale'})
        self.assertEqual(response.status_code, 409)
        self.assertContains(self.client.get(reverse('core:recruitment_list'), {'department':'editors'}), 'anna@example.com')
        self.assertNotContains(self.client.get(reverse('core:recruitment_list'), {'department':'reviewers'}), 'anna@example.com')

    def test_single_review_preserves_warning_confirmation(self):
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.get(reverse('core:review_create')).status_code, 403)
        self.client.force_login(self.superuser)
        self.author.is_blacklisted = True
        self.author.save()
        data = {'author': self.author.pk, 'title':'Test warning', 'genre':'fantasy', 'length':123, 'anthology':self.anthology.pk}
        response = self.client.post(reverse('core:review_create'), data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Review.objects.count(), 0)
        data['confirm_submission_warnings'] = 'on'
        data['submission_warnings_token'] = response.context['form'].data['submission_warnings_token']
        response = self.client.post(reverse('core:review_create'), data)
        self.assertEqual(response.status_code, 302)
        review = Review.objects.get()
        self.assertTrue(review.is_hidden)
        self.assertEqual(review.status, Review.Status.REJECTED)

    def test_editor_activity_and_access(self):
        text = Text.objects.create(title='Editor report', length=1, anthology=self.anthology)
        WorkflowStage.objects.create(text=text, stage_type='editing', started_at=timezone.localdate())
        WorkflowRoleAssignment.objects.create(text=text, role='editor', assigned_to=self.editor)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse('core:editor_activity')).status_code, 403)
        self.client.force_login(self.coordinator)
        self.assertContains(self.client.get(reverse('core:editor_activity')), 'Editor report')

    def test_new_pages_render(self):
        self.client.force_login(self.superuser)
        for route in ('extract_list','extract_add','recruitment_list','recruitment_add','review_create','editor_activity','text_list','reviewer_activity','anthology_corrections'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(f'core:{route}')).status_code, 200)


from unittest import skipUnless
from django.db import connection, IntegrityError, transaction
from authors.models import Author


@skipUnless(connection.vendor == 'mysql', 'Wymaga produkcyjnego silnika MySQL 8')
class MySQLCaseInsensitiveTests(TestCase):
    def test_unicode_equality_and_search(self):
        text = Text.objects.create(title='ŻÓŁW', length=1)
        self.assertTrue(Text.objects.filter(title='żółw', pk=text.pk).exists())
        self.assertTrue(Text.objects.filter(title__icontains='żółw', pk=text.pk).exists())

    def test_unique_email_is_case_insensitive_in_database(self):
        Author.objects.create(first_name='A', last_name='B', email='case@example.com')
        with self.assertRaises(IntegrityError), transaction.atomic():
            # bulk_create omija normalizację modelu: sprawdzamy samą bazę.
            Author.objects.bulk_create([Author(first_name='C', last_name='D', email='CASE@example.com')])
