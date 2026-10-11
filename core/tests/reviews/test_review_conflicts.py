"""Konflikty wersji przy zapisie recenzji: strona konfliktu nie ujawnia ukrytych zgłoszeń ani danych prywatnych."""
from django.conf import settings
from django.test import Client
from django.urls import reverse

from fantazmaty.testing import form_value
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from people.models import Role
from texts.models import Review, ReviewAssignment


class ReviewConflictTests(StatusAssignmentFixtures):
    def role(self, user, name):
        user.person_profile.roles.add(Role.objects.get_or_create(name=name)[0])

    def csrf_client(self, user):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        client.get(reverse('password_change'))
        return client, client.cookies[settings.CSRF_COOKIE_NAME].value

    def test_hidden_review_conflict_respects_visibility_with_valid_csrf(self):
        self.role(self.other, 'Koordynator')
        review = Review.objects.create(title='Private hidden title', content_warnings='Private warnings',
                                       length=100, anthology=self.book, is_hidden=True)
        for user, endpoint in ((self.member, 'assign_reviewer'), (self.other, 'update_review_status')):
            client, csrf = self.csrf_client(user)
            for version in (None, 'invalid'):
                with self.subTest(user=user.pk, version=version):
                    data = {'status': 'accepted'}
                    if version is not None:
                        data['_edit_version'] = version
                    response = client.post(reverse('core:' + endpoint, args=[review.pk]), data,
                                           HTTP_X_CSRFTOKEN=csrf)
                    self.assertIn(response.status_code, (403, 404))
                    self.assertNotIn(b'Private hidden title', response.content)
                    self.assertNotIn(b'Private warnings', response.content)
        review.refresh_from_db()
        self.assertEqual(review.status, Review.Status.NEW)
        self.assertFalse(review.assignments.exists())

    def test_superuser_retains_conflict_for_hidden_review(self):
        review = Review.objects.create(title='Hidden for ordinary users', length=100,
                                       anthology=self.book, is_hidden=True)
        client, csrf = self.csrf_client(self.admin)
        response = client.post(reverse('core:update_review_status', args=[review.pk]),
                               {'_edit_version': 'invalid'}, HTTP_X_CSRFTOKEN=csrf)
        self.assertContains(response, review.title, status_code=409)

    def test_visible_review_retains_conflict_for_coordinator(self):
        self.role(self.member, 'Koordynator')
        review = Review.objects.create(title='Visible review', length=100, anthology=self.book)
        self.client.force_login(self.member)
        response = self.client.post(reverse('core:update_review_status', args=[review.pk]),
                                    {'_edit_version': 'invalid'})
        self.assertContains(response, review.title, status_code=409)

    def test_archive_conflict_does_not_bypass_admin_only_withdrawn_reviews(self):
        """Archiwum widzi cały zespół; wycofane zgłoszenia zostają tylko w panelu admina."""
        review = Review.objects.create(title='Private withdrawn entry', length=100,
                                       anthology=self.book, status='withdrawn')
        self.client.force_login(self.member)
        response = self.client.post(reverse('core:assign_reviewer', args=[review.pk]),
                                    {'_edit_version': 'invalid'})
        self.assertIn(response.status_code, (403, 404))
        self.assertNotIn(review.title.encode(), response.content)

    def test_reviewer_conflict_does_not_reveal_private_fields(self):
        self.role(self.member, 'Recenzent')
        review = Review.objects.create(
            title='Zgłoszenie', anthology=self.book, length=1000, author_first_name='Tajny',
            author_last_name='Autor', email='secret@example.test', author_message='SECRET_MESSAGE',
            file_url='https://www.dropbox.com/secret-folder')
        ReviewAssignment.objects.create(review=review, user=self.member, position=1, opinion='yes',
                                        notes='Oddana opinia')
        detail = reverse('core:assigned_review_detail', args=[review.pk])
        self.client.force_login(self.member)
        token = form_value(self.client.get(detail))
        review.title = 'Zmiana'
        review.save()
        page = self.client.post(detail, {'opinion': 'maybe', 'notes': 'Nowa', '_edit_version': token})
        self.assertEqual(page.status_code, 409)
        for secret in ('secret-folder', 'SECRET_MESSAGE', 'secret@example.test'):
            self.assertNotContains(page, secret, status_code=409)
        self.assertContains(page, 'Oddana opinia', status_code=409)
