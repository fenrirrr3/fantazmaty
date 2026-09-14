from datetime import timedelta
from importlib import import_module
from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from authors.models import Author
from people.models import Person, Role, Vacation
from texts.models import Anthology, Review, ReviewAssignment, Text
from texts.admin import ReviewAdminForm, ReviewAssignmentInline
from core.forms import ReviewBulkImportForm
from core.intake_forms import SingleReviewForm
from core.models import AnthologyCorrection
from core.services.vacations import cancel_planned_vacation


class RevisionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser('root', 'root@example.test', 'password')
        self.user = User.objects.create_user('member', 'member@example.test', 'password')
        self.person = Person.objects.create(first_name='Jan', last_name='Testowy', email=self.user.email, user=self.user)
        role, _ = Role.objects.get_or_create(name='Recenzent')
        self.person.roles.add(role)
        self.anthology = Anthology.objects.create(title='Nabór')
        self.published = Anthology.objects.create(title='Wydana', status='published')
        self.author = Author.objects.create(first_name='Łukasz', last_name='Żółć', pseudonym='Zielony Smok', email='author@example.test')
        self.client.force_login(self.user)

    def review(self, **kwargs):
        data = dict(title='Żółty tekst', anthology=self.anthology, author=self.author,
                    author_first_name=self.author.first_name, author_last_name=self.author.last_name,
                    email=self.author.email, genre='Fantasy', length=100)
        data.update(kwargs)
        return Review.objects.create(**data)

    def test_published_intake_choices_and_validation(self):
        for form in (SingleReviewForm(), ReviewBulkImportForm(user=self.root), ReviewAdminForm()):
            self.assertNotIn(self.published, form.fields['anthology'].queryset)
            with self.assertRaises(ValidationError):
                form.fields['anthology'].clean(str(self.published.pk))
        archived = self.review(anthology=self.published, old_reviews=True)
        self.assertIn(self.published, ReviewAdminForm(instance=archived).fields['anthology'].queryset)

    def test_author_suggestions_pseudonym_and_permissions(self):
        url = reverse('core:author_suggestions')
        self.assertEqual(self.client.get(url, {'q': 'Smok'}).status_code, 403)
        self.client.force_login(self.root)
        for query in ['zielony smok', 'Lukasz Zolc', 'author@example.test']:
            response = self.client.get(url, {'q': query})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['results'][0]['id'], self.author.pk)
            self.assertEqual(response.json()['results'][0]['first_name'], 'Łukasz')
        self.assertEqual(self.client.get(url).json(), {'results': []})

    def test_single_form_autofills_on_server(self):
        form = SingleReviewForm(data={'author':self.author.pk, 'title':'Nowy', 'genre':'Fantasy', 'length':123, 'anthology':self.anthology.pk})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['email'], self.author.email)
        self.assertEqual(form.cleaned_data['author_last_name'], 'Żółć')

    def test_my_reviews_filter(self):
        ReviewAssignment.objects.create(review=self.review(), user=self.user, position=1)
        response = self.client.get(reverse('core:my_reviews'), {'q':'zolty'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Żółty tekst')
        response = self.client.get(reverse('core:my_reviews'), {'q':'nieistniejący'})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Żółty tekst')

    def test_coordinator_role_promotes_staff_not_superuser(self):
        role, _ = Role.objects.get_or_create(name='Koordynator ilustracji')
        self.person.roles.add(role)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.user.is_staff = False
        self.user.save()
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)

    def test_group_and_reverse_role_promote_staff(self):
        group = Group.objects.create(name='Koordynator testów')
        self.user.groups.add(group)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        other = get_user_model().objects.create_user('other')
        person = Person.objects.create(first_name='A', last_name='B', email='other@example.test', user=other)
        role, _ = Role.objects.get_or_create(name='Koordynator')
        role.people.add(person)
        other.refresh_from_db()
        self.assertTrue(other.is_staff)

    def test_coordinator_migration_preserves_access(self):
        role = Role.objects.create(name='Koordynator zespołu')
        self.person.roles.add(role)
        import_module('people.migrations.0007_coordinator_staff').migrate_coordinators(apps, connection.schema_editor())
        self.assertFalse(Role.objects.filter(name='Koordynator zespołu').exists())
        self.assertTrue(self.person.roles.filter(name='Koordynator').exists())

    def test_future_vacation_deleted_without_history(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        vacation = Vacation.objects.create(person=self.person, start_date=tomorrow, until_revoked=True)
        self.person.leave_start_date = tomorrow
        self.person.leave_until_revoked = True
        self.person.save()
        url = reverse('core:cancel_vacation', args=[vacation.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(Vacation.objects.filter(pk=vacation.pk).exists())
        self.person.refresh_from_db()
        self.assertIsNone(self.person.leave_start_date)
        self.assertFalse(self.person.leave_until_revoked)

    def test_cannot_cancel_started_or_others_vacation(self):
        vacation = Vacation.objects.create(person=self.person, start_date=timezone.localdate(), until_revoked=True)
        with self.assertRaises(ValidationError):
            cancel_planned_vacation(user=self.user, vacation_id=vacation.pk)
        other = get_user_model().objects.create_user('other')
        Person.objects.create(first_name='A', last_name='B', email='other@example.test', user=other)
        with self.assertRaises(PermissionDenied):
            cancel_planned_vacation(user=other, vacation_id=vacation.pk)
        self.assertTrue(Vacation.objects.filter(pk=vacation.pk).exists())

    def correction(self):
        return AnthologyCorrection.objects.create(anthology=self.anthology, submitted_by=self.user,
                     story_title='Inne miejsce', fragment='Fragment', problem='Problem', suggestion='Poprawka')

    def test_own_correction_edit_delete_and_stale_version(self):
        item = self.correction()
        url = reverse('core:correction_edit', args=[item.pk])
        version = item.updated_at.isoformat()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = {'anthology':self.anthology.pk, 'fragment':'Nowy', 'problem':'Problem', 'suggestion':'Poprawka', 'version':version}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.fragment, 'Nowy')
        delete = reverse('core:correction_delete', args=[item.pk])
        self.assertEqual(self.client.get(delete).status_code, 405)
        self.assertEqual(self.client.post(delete, {'version':version}).status_code, 409)
        self.assertEqual(self.client.post(delete, {'version':item.updated_at.isoformat()}).status_code, 302)
        self.assertFalse(AnthologyCorrection.objects.filter(pk=item.pk).exists())

    def test_other_user_cannot_change_correction(self):
        item = self.correction()
        self.client.force_login(self.root)
        self.assertEqual(self.client.post(reverse('core:correction_delete', args=[item.pk]), {'version':item.updated_at.isoformat()}).status_code, 404)
        self.assertEqual(self.client.get(reverse('core:correction_edit', args=[item.pk])).status_code, 404)

    def test_archive_visible_in_person_profile_without_account(self):
        person = Person.objects.create(first_name='Historyczny', last_name='Recenzent', email='history@example.test', is_active=False)
        ReviewAssignment.objects.create(review=self.review(old_reviews=True), historical_person=person, position=1, assigned_at=None, opinion_changed_at=None)
        response = self.client.get(reverse('core:person_detail', args=[person.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Archiwalne recenzje')
        self.assertContains(response, 'Żółty tekst')
        self.assertNotContains(response, self.author.email)

    def test_archive_admin_inlines_and_dates(self):
        review = self.review(old_reviews=True)
        assignment = ReviewAssignment.objects.create(review=review, historical_person=self.person, position=1, assigned_at=None, opinion_changed_at=None)
        request = RequestFactory().get('/')
        request.user = self.root
        inline = ReviewAssignmentInline(Review, admin.site)
        self.assertTrue(inline.has_change_permission(request, review))
        self.assertTrue(inline.has_delete_permission(request, review))
        self.client.force_login(self.root)
        response = self.client.get(reverse('admin:texts_review_change', args=[review.pk]))
        self.assertEqual(response.status_code, 200)
        assignment.opinion = ReviewAssignment.Opinion.YES
        assignment.save()
        self.assertIsNone(assignment.opinion_changed_at)
        response = self.client.post(reverse('admin:texts_review_delete', args=[review.pk]), {'post':'yes'})
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        self.assertFalse(Review.objects.filter(pk=review.pk).exists())

    def test_admin_post_edits_archived_review_and_assignment(self):
        review = self.review(old_reviews=True)
        assignment = ReviewAssignment.objects.create(review=review, historical_person=self.person, position=1, assigned_at=None, opinion_changed_at=None)
        self.client.force_login(self.root)
        url = reverse('admin:texts_review_change', args=[review.pk])
        page = self.client.get(url)
        data = {}
        def add_form(form):
            for field in form:
                value = field.value()
                if value is None or value is False:
                    continue
                if hasattr(value, 'values_list'):
                    value = list(value.values_list('pk', flat=True))
                data[field.html_name] = value
        add_form(page.context['adminform'].form)
        for inline in page.context['inline_admin_formsets']:
            add_form(inline.formset.management_form)
            for form in inline.formset.forms:
                add_form(form)
        data['title'] = 'Poprawiony tytuł archiwalny'
        data['assignments-0-opinion'] = ReviewAssignment.Opinion.YES
        data['_save'] = 'Zapisz'
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, str(response.context['adminform'].form.errors) if response.context else '')
        review.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(review.title, 'Poprawiony tytuł archiwalny')
        self.assertIsNone(review.decision_at)
        self.assertEqual(assignment.opinion, ReviewAssignment.Opinion.YES)
        self.assertIsNone(assignment.opinion_changed_at)

    def test_archive_report_scope(self):
        review = self.review(old_reviews=True)
        ReviewAssignment.objects.create(review=review, historical_person=self.person, position=1, assigned_at=None, opinion_changed_at=None)
        self.client.force_login(self.root)
        url = reverse('core:reviewer_activity')
        self.assertNotContains(self.client.get(url), review.title)
        self.assertContains(self.client.get(url, {'archive':'archived'}), review.title)

    def test_programs_and_corrections_pages(self):
        self.correction()
        self.assertEqual(self.client.get(reverse('core:anthology_corrections')).status_code, 200)
        self.assertContains(self.client.get(reverse('core:programs')), 'you-shall-not-pass-gandalf-lotr.gif')
