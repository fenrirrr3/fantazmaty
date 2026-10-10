"""Panel administracyjny a strona (10.10): poprawki rozbieżności i ogólnych ryzyk."""
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.edit_versions import version_of
from core.models import PostLayoutAssignment
from people.models import Person, Role, Vacation
from texts.models import Anthology, Review, ReviewAssignment, Text
from workflow.admin_assignment_edit import correct_assignment
from workflow.admin_stage_dates import preserve_stage_order
from workflow.models import WorkflowRoleAssignment as A, WorkflowStage as S


class AdminWorkflowTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        override.enable()
        self.addCleanup(override.disable)
        self.admin = get_user_model().objects.create_superuser('admin10', 'admin10@example.com', 'test')
        self.user = get_user_model().objects.create_user('editor10', 'editor10@example.com', 'test')
        self.person = Person.objects.create(user=self.user, email=self.user.email, first_name='Ewa', last_name='Redaktor')
        self.person.roles.add(Role.objects.get_or_create(name='Redaktor')[0])
        self.book = Anthology.objects.create(title='Antologia testowa')
        self.text = Text.objects.create(title='Tekst', length=100, anthology=self.book)
        self.today = timezone.localdate()

    def test_clearing_an_assignment_with_finished_work_is_refused(self):
        assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.user)
        S.objects.create(text=self.text, stage_type='editing', assignment=assignment, is_completed=True,
                         started_at=self.today, ended_at=self.today)
        with self.assertRaisesMessage(ValidationError, 'zakończone etapy'):
            correct_assignment(assignment.pk, self.admin, version_of(self.text), action='clear')
        assignment.refresh_from_db()
        self.assertEqual(assignment.assigned_to, self.user)

    def test_date_correction_cannot_reorder_stages(self):
        first = S.objects.create(text=self.text, stage_type='editing', is_completed=True,
                                 started_at=self.today - timedelta(days=10), ended_at=self.today - timedelta(days=8))
        S.objects.create(text=self.text, stage_type='first_verification', is_completed=True,
                         started_at=self.today - timedelta(days=7), ended_at=self.today - timedelta(days=5))
        with self.assertRaises(ValidationError):
            preserve_stage_order(first, self.today - timedelta(days=10), self.today - timedelta(days=6))
        preserve_stage_order(first, self.today - timedelta(days=11), self.today - timedelta(days=7))

    def test_text_with_recorded_work_cannot_be_deleted_in_admin(self):
        from texts.admin import TextAdmin
        request = RequestFactory().get('/')
        request.user = self.admin
        model_admin = TextAdmin(Text, site)
        self.assertTrue(model_admin.has_delete_permission(request, self.text))
        S.objects.create(text=self.text, stage_type='editing', started_at=self.today)
        self.assertFalse(model_admin.has_delete_permission(request, self.text))
        self.assertNotIn('delete_selected', model_admin.get_actions(request))

    def test_deleting_text_detaches_its_source_review(self):
        from texts.admin import TextAdmin
        review = Review.objects.create(title='Źródło', length=100, anthology=self.book, status='accepted',
                                       author_first_name='A', author_last_name='B', email='a@example.test',
                                       copied_text=self.text)
        request = RequestFactory().post('/')
        request.user = self.admin
        TextAdmin(Text, site).delete_model(request, self.text)
        review.refresh_from_db()
        self.assertTrue(review.publication_detached)
        self.assertIsNone(review.copied_text_id)


class AdminReviewTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('admin11', 'admin11@example.com', 'test')
        self.reviewer = get_user_model().objects.create_user('rev11', 'rev11@example.com', 'test')
        self.book = Anthology.objects.create(title='Antologia recenzji')
        self.review = Review.objects.create(title='Zgłoszenie', length=10, anthology=self.book, status='in_review',
                                            author_first_name='A', author_last_name='B', email='b@example.test')
        self.request = RequestFactory().get('/')
        self.request.user = self.admin

    def model_admin(self):
        from texts.admin import ReviewAdmin
        return ReviewAdmin(Review, site)

    def test_hidden_flag_is_read_only_for_current_reviews(self):
        self.assertIn('is_hidden', self.model_admin().get_readonly_fields(self.request, self.review))

    def test_review_with_submitted_opinion_cannot_be_deleted(self):
        self.assertTrue(self.model_admin().has_delete_permission(self.request, self.review))
        ReviewAssignment.objects.create(review=self.review, user=self.reviewer, opinion='yes', position=1)
        self.assertFalse(self.model_admin().has_delete_permission(self.request, self.review))
        self.assertNotIn('delete_selected', self.model_admin().get_actions(self.request))

    def test_superuser_corrects_rejection_before_the_author_is_notified(self):
        from core.services.reviews import _validate_status_change
        self.review.status = Review.Status.REJECTED
        self.review.save()
        _validate_status_change(user=self.admin, review=self.review, assignments=[], new_status=Review.Status.ACCEPTED)
        self.review.author_notified_at = timezone.now()
        with self.assertRaises(ValidationError):
            _validate_status_change(user=self.admin, review=self.review, assignments=[], new_status=Review.Status.ACCEPTED)


class AdminOtherModelsTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('admin12', 'admin12@example.com', 'test')
        self.request = RequestFactory().post('/')
        self.request.user = self.admin

    def test_post_layout_admin_delete_keeps_history(self):
        from core.admin import PostLayoutAssignmentAdmin
        reader = get_user_model().objects.create_user('pl12', 'pl12@example.com', 'test')
        book = Anthology.objects.create(title='Poskładowa')
        item = PostLayoutAssignment.objects.create(anthology=book, proofreader=reader, created_by=self.admin,
                                                   assigned_start=timezone.localdate())
        model_admin = PostLayoutAssignmentAdmin(PostLayoutAssignment, site)
        model_admin.delete_model(self.request, item)
        item.refresh_from_db()
        self.assertIsNotNone(item.deleted_at)
        self.assertFalse(model_admin.has_change_permission(self.request, item))

    def test_planned_vacation_of_former_member_can_be_deleted(self):
        from core.admin import VacationAdmin
        person = Person.objects.create(first_name='Była', last_name='Osoba', email='byla@example.com')
        vacation = Vacation.objects.create(person=person, start_date=timezone.localdate() + timedelta(days=10),
                                           end_date=timezone.now() + timedelta(days=12))
        person.is_active = False
        person.save()
        VacationAdmin(Vacation, site).delete_model(self.request, vacation)
        self.assertFalse(Vacation.objects.filter(pk=vacation.pk).exists())

    def test_account_names_fit_the_person_profile(self):
        from core.admin import AccountAdmin
        user = get_user_model().objects.create_user('acc12', 'acc12@example.com', 'test')
        request = RequestFactory().get('/')
        request.user = self.admin
        form_class = AccountAdmin(get_user_model(), site).get_form(request, user)
        form = form_class(instance=user)
        self.assertEqual(form.fields['last_name'].max_length, Person._meta.get_field('last_name').max_length)

    def test_only_one_active_recruitment_mailbox(self):
        from core.admin import MailboxConnectionForm
        from core.models import MailboxConnection
        first = MailboxConnection(name='Rekrutacja', purpose=MailboxConnection.Purpose.RECRUITMENT, host='imap.example.com',
                                  port=993, username='a@example.com', folder='INBOX', is_active=True)
        first.set_password('secret')
        first.save()
        form = MailboxConnectionForm(data={
            'name': 'Druga', 'purpose': MailboxConnection.Purpose.RECRUITMENT, 'host': 'imap.example.com',
            'port': 993, 'security': first.security, 'username': 'b@example.com', 'password': 'x', 'folder': 'INBOX',
            'recruitment_subjects': '', 'is_active': 'on'})
        self.assertFalse(form.is_valid())
        self.assertIn('tylko jedna skrzynka rekrutacji', str(form.errors))


class LoginThrottleTests(TestCase):
    def test_owner_with_correct_password_is_not_locked_out(self):
        from core import auth_throttle
        user = get_user_model().objects.create_user('owner12', 'owner12@example.test', 'valid-password')
        bad = {'username': user.email, 'password': 'incorrect'}
        with override_settings(AUTH_THROTTLE_CLIENT_IP_HEADER='HTTP_X_REAL_IP'), \
                patch.object(auth_throttle, 'ACCOUNT_LOGIN_LIMIT', 3):
            for i in range(3):
                self.client.post(reverse('login'), bad, HTTP_X_REAL_IP=f'192.0.2.{i}')
            self.assertEqual(self.client.post(reverse('login'), bad, HTTP_X_REAL_IP='198.51.100.7').status_code, 429)
            good = {'username': user.email, 'password': 'valid-password'}
            self.assertEqual(self.client.post(reverse('login'), good, HTTP_X_REAL_IP='198.51.100.8').status_code, 302)


class MinorAdminAlignmentTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('admin13', 'admin13@example.com', 'test')
        self.book = Anthology.objects.create(title='Antologia AD')

    def test_audio_description_task_ready_requires_content(self):
        from core.admin import AnthologyTaskAdminForm
        task = self.book.production_tasks.get(task_type='audio_description')
        form = AnthologyTaskAdminForm(data={'status': 'ready', 'assigned_to': self.admin.pk}, instance=task)
        form.is_valid()
        self.assertIn('status', form.errors)
        description = self.book.audio_description
        description.content = 'Opis okładki'
        description.save()
        form = AnthologyTaskAdminForm(data={'status': 'ready', 'assigned_to': self.admin.pk}, instance=task)
        form.is_valid()
        self.assertNotIn('status', form.errors)

    def test_audio_description_admin_offers_people_without_account(self):
        from core.audio_description_admin import AudioDescriptionAdminForm
        person = Person.objects.create(first_name='Bez', last_name='Konta', email='bez@example.com')
        form = AudioDescriptionAdminForm(instance=self.book.audio_description)
        self.assertIn(person, form.fields['controllers'].queryset)

    def test_audiobook_proofreader_locked_outside_proofreading(self):
        from core.admin import AudiobookAdmin
        from core.models import Audiobook
        text = Text.objects.create(title='Nagranie', length=1, anthology=self.book)
        audio = Audiobook.objects.create(text=text, status='recording')
        request = RequestFactory().get('/')
        request.user = self.admin
        self.assertIn('proofreader', AudiobookAdmin(Audiobook, site).get_readonly_fields(request, audio))

    def test_coordinator_gets_author_profile_links_on_reviews(self):
        from core.views.reviews import _permission_context
        coordinator = get_user_model().objects.create_user('coord13', 'coord13@example.com', 'test')
        person = Person.objects.create(user=coordinator, email=coordinator.email, first_name='K', last_name='O')
        person.roles.add(Role.objects.get_or_create(name='Koordynator redakcji')[0])
        self.assertTrue(_permission_context(coordinator)['can_open_author_profiles'])
