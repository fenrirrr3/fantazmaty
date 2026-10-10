"""Procesy użytkowników (11.10): oddawanie etapów, potwierdzenia, recenzje bez opinii."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from core.services.texts import change_scheduled_stage, restore_withdrawn_text, withdraw_text
from core.test_status_assignment_regression import StatusAssignmentFixtures
from people.models import Person, Role, Vacation
from texts.models import Review, ReviewAssignment, Reviewers
from workflow.handoffs import handoff_stage
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S


class StageReleaseTests(StatusAssignmentFixtures):
    def setUp(self):
        super().setUp()
        for user in (self.member, self.other):
            user.person_profile.roles.add(Role.objects.get_or_create(name='Redaktor')[0])
        self.verifier = get_user_model().objects.create_user('ver', 'ver@example.com', 'test')
        Person.objects.create(user=self.verifier, first_name='Wera', last_name='Weryfikator', email=self.verifier.email)
        self.editor = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        self.editing = S.objects.create(text=self.text, stage_type='editing', assignment=self.editor,
                                        started_at=self.today)
        self.v1 = A.objects.create(text=self.text, role='verifier_1', assigned_to=self.verifier)
        self.first = S.objects.create(text=self.text, stage_type='first_verification', assignment=self.v1)

    def detail(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk])).content.decode()

    def test_editor_gives_back_started_editing(self):
        self.assertIn('Cofnij przydział', self.detail(self.member))
        change_scheduled_stage(user=self.member, stage_id=self.editing.pk, cancel=True)
        self.editing.refresh_from_db()
        self.editor.refresh_from_db()
        self.assertEqual(self.editing.stage_type, 'ready_for_editing')
        self.assertIsNone(self.editing.started_at)
        self.assertIsNone(self.editor.assigned_to_id)

    def test_verifier_cancels_first_verification_reservation(self):
        self.assertIn('Odwołaj rezerwację', self.detail(self.verifier))
        change_scheduled_stage(user=self.verifier, stage_id=self.first.pk, cancel=True)
        self.v1.refresh_from_db()
        self.assertIsNone(self.v1.assigned_to_id)

    def test_editing_cannot_be_given_back_once_verification_started(self):
        S.objects.filter(pk=self.editing.pk).update(is_completed=True, ended_at=self.today)
        S.objects.filter(pk=self.first.pk).update(started_at=self.today)
        second = S.objects.create(text=self.text, stage_type='editing', iteration=2, assignment=self.editor,
                                  started_at=self.today)
        with self.assertRaises(ValidationError):
            change_scheduled_stage(user=self.member, stage_id=second.pk, cancel=True)

    def test_other_member_cannot_release_and_gets_a_message(self):
        self.client.force_login(self.other)
        response = self.client.post(reverse('core:change_scheduled_workflow_stage', args=[self.editing.pk]),
                                    {'action': 'cancel'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.editing.refresh_from_db()
        self.assertIsNotNone(self.editing.started_at)

    def test_new_editor_can_start_after_handoff(self):
        handoff_stage(self.text, self.admin, stage_id=self.editing.pk, assigned_to_id=self.other.pk,
                      expected_assignment_id=self.editor.pk, reason='Zmiana')
        page = self.detail(self.other)
        self.assertIn(reverse('core:start_assigned_workflow_stage', args=[self.editing.pk]), page)

    def test_warning_when_reserved_verifier_is_on_leave(self):
        Vacation.objects.create(person=self.verifier.person_profile, start_date=self.today - timedelta(days=1),
                                end_date=timezone.now() + timedelta(days=5))
        self.assertIn('jest na urlopie', self.detail(self.member))

    def test_confirmations_and_finish_date_are_rendered(self):
        page = self.detail(self.member)
        self.assertIn('data-confirm="Przekazać tekst do pierwszej weryfikacji?', page)
        self.assertIn('pierwszej weryfikacji. Jeśli nie ma weryfikatora', page)


class WithdrawalUndoTests(StatusAssignmentFixtures):
    def test_superuser_restores_withdrawn_text(self):
        S.objects.create(text=self.text, stage_type='editing', started_at=self.today,
                         assignment=A.objects.create(text=self.text, role='editor', assigned_to=self.admin))
        withdraw_text(user=self.admin, text_id=self.text.pk)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk])).content.decode()
        self.assertIn('Przywróć do procesu', page)
        restore_withdrawn_text(user=self.admin, text_id=self.text.pk)
        self.assertFalse(S.objects.filter(text=self.text, stage_type='withdrawn').exists())

    def test_member_cannot_restore(self):
        from django.core.exceptions import PermissionDenied
        withdraw_text(user=self.admin, text_id=self.text.pk)
        with self.assertRaises(PermissionDenied):
            restore_withdrawn_text(user=self.member, text_id=self.text.pk)


class ReviewProcessTests(StatusAssignmentFixtures):
    def setUp(self):
        super().setUp()
        self.member.person_profile.roles.add(Role.objects.get_or_create(name='Koordynator recenzji')[0])
        self.review = Review.objects.create(title='Zgłoszenie zaległe', length=10, anthology=self.book,
                                            status=Review.Status.IN_REVIEW, author_first_name='A',
                                            author_last_name='B', email='a@example.test')

    def assign(self, user, days, opinion=Reviewers.Opinion.READING, position=1):
        item = ReviewAssignment.objects.create(review=self.review, user=user, opinion=opinion, position=position)
        ReviewAssignment.objects.filter(pk=item.pk).update(assigned_at=timezone.now() - timedelta(days=days))
        return item

    def test_coordinator_releases_reading_slot(self):
        from core.services.reviews import release_reviewer_slot
        item = self.assign(self.other, 30)
        release_reviewer_slot(user=self.member, review_id=self.review.pk, assignment_id=item.pk)
        self.assertFalse(ReviewAssignment.objects.filter(pk=item.pk).exists())
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, Review.Status.NEW)

    def test_submitted_opinion_slot_cannot_be_released(self):
        from core.services.reviews import release_reviewer_slot
        item = self.assign(self.other, 30, opinion='yes')
        with self.assertRaises(ValidationError):
            release_reviewer_slot(user=self.member, review_id=self.review.pk, assignment_id=item.pk)

    def test_overdue_reviews_are_listed_in_inactivity_report(self):
        from core.selectors.reports import overdue_review_rows
        late = self.assign(self.other, 22)
        self.assign(self.admin, 10, position=2)
        rows = overdue_review_rows()
        self.assertEqual([row['review'].pk for row in rows], [self.review.pk])
        self.assertEqual(rows[0]['days'], 22)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:workflow_inactivity')).content.decode()
        self.assertIn('Zgłoszenie zaległe', page)
        page = self.client.get(reverse('core:workflow_inactivity'), {'mode': 'reviews'}).content.decode()
        self.assertIn('Zgłoszenie zaległe', page)
        self.assertNotIn('workflow-inactivity-table', page)
        ReviewAssignment.objects.filter(pk=late.pk).update(opinion='yes')
        self.assertEqual(overdue_review_rows(), [])

    def test_accepted_decision_locked_after_author_notification(self):
        from core.services.reviews import _validate_status_change
        self.review.status = Review.Status.ACCEPTED
        self.review.author_notified_at = timezone.now()
        with self.assertRaisesMessage(ValidationError, 'powiadomiony o przyjęciu'):
            _validate_status_change(user=self.member, review=self.review, assignments=[],
                                    new_status=Review.Status.REJECTED)


class ReviewListRegressionTests(StatusAssignmentFixtures):
    def test_sorting_by_author_renders_rows(self):
        review = Review.objects.create(title='Sortowane', length=10, anthology=self.book, status=Review.Status.IN_REVIEW,
                                       author_first_name='Ala', author_last_name='Kot', email='k@example.test')
        ReviewAssignment.objects.create(review=review, user=self.admin, opinion=Reviewers.Opinion.READING, position=1)
        self.client.force_login(self.admin)
        for sort in ('author', '-author'):
            response = self.client.get(reverse('core:review_list'), {'sort': sort})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Sortowane')

    def test_status_filter_offers_only_supported_statuses(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:review_list'))
        values = [value for value, _label in response.context['status_choices']]
        self.assertNotIn(Review.Status.WITHDRAWN, values)
        self.assertIn(Review.Status.NEW, values)


class AdminWorkflowCycleTests(StatusAssignmentFixtures):
    """Numer przebiegu jest w adminie tylko do odczytu – formularz nie może go zmienić."""

    def test_cycle_cannot_be_changed_in_admin_forms(self):
        assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.member)
        stage = S.objects.create(text=self.text, stage_type='editing', assignment=assignment, started_at=self.today)
        self.client.force_login(self.admin)
        for model, obj in (('workflowroleassignment', assignment), ('workflowstage', stage)):
            url = reverse(f'admin:workflow_{model}_change', args=[obj.pk])
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'name="workflow_cycle"')
            import re
            token = re.search(r'name="_edit_version" value="([^"]+)"', page.content.decode())
            data = {'workflow_cycle': 7, 'notes': 'Uwaga'}
            if token:
                data['_edit_version'] = token.group(1)
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 302)
            obj.refresh_from_db()
            self.assertEqual(obj.workflow_cycle, 1)
        assignment.refresh_from_db()
        self.assertEqual(assignment.notes, 'Uwaga')


class SharedTemplatesTests(StatusAssignmentFixtures):
    def test_author_search_script_only_on_intake_page(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('core:review_create')), 'core/author-search.js')
        self.assertNotContains(self.client.get(reverse('core:home')), 'core/author-search.js')

    def test_activity_reports_keep_their_own_labels(self):
        self.client.force_login(self.admin)
        for name, title, prefix in (('editor_activity', 'Aktywność redaktorów', 'editor'),
                                    ('proofreader_activity', 'Aktywność korektorów', 'proofreader'),
                                    ('verifier_activity', 'Aktywność weryfikatorów', 'verifier')):
            page = self.client.get(reverse('core:' + name)).content.decode()
            self.assertIn(f'<h1 class="page-title">{title}</h1>', page)
            self.assertIn(f'id="{prefix}-activity-table"', page)
            self.assertIn(f'action="{reverse("core:" + name)}"', page)
        self.assertIn('Zakończony etap nie oznacza gotowej redakcji',
                      self.client.get(reverse('core:editor_activity')).content.decode())
