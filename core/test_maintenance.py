from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.http import HttpResponse
from django.test import TestCase, RequestFactory, Client
from django.urls import reverse, resolve
from django.utils import timezone

from authors.models import Author, AuthorNote, BlacklistEntry
from core.activity import UserActivityMiddleware, describe_request
from core.forms import AuthorNoteForm, ReviewBulkImportForm
from core.models import UserActivity
from core.services.reviews import import_reviews, change_review_status, perform_bulk_review_action
from texts.models import Anthology, Review, Text
from texts.blacklist import apply_blacklist


class MaintenanceTests(TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory
        self.spool = TemporaryDirectory()
        self.addCleanup(self.spool.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=self.spool.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_superuser('maintenance', 'maintenance@example.com', 'unused-test-password')
        self.book = Anthology.objects.create(title='Test naboru')
        self.author = Author.objects.create(first_name='Jan', last_name='Testowy', email='jan@example.com')
        self.factory = RequestFactory()

    def review(self, **kwargs):
        values = dict(author=self.author, author_first_name='Jan', author_last_name='Testowy', email=self.author.email,
                      title='Test', length=1000, anthology=self.book)
        values.update(kwargs)
        return Review.objects.create(**values)

    def test_empty_new_note_but_existing_can_be_cleared(self):
        self.assertFalse(AuthorNoteForm({'content': '   '}).is_valid())
        note = AuthorNote.objects.create(author=self.author, content='Old', created_by=self.user)
        form = AuthorNoteForm({'content': ''}, instance=note)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        note.refresh_from_db()
        self.assertEqual(note.content, '')

    def test_blacklisted_coauthor(self):
        coauthor = Author.objects.create(first_name='Anna', last_name='Test', email='anna@example.com')
        BlacklistEntry.objects.create(email='anna@example.com')
        review = self.review()
        review.coauthors.add(coauthor)
        apply_blacklist(review)
        self.assertTrue(review.is_hidden)
        self.assertEqual(review.status, Review.Status.REJECTED)
        unsaved = Review(author=self.author, email=self.author.email)
        apply_blacklist(unsaved, coauthors=[coauthor])
        self.assertTrue(unsaved.is_hidden)

    def test_rejected_decision_blocked_in_both_paths(self):
        review = self.review(status=Review.Status.REJECTED)
        with self.assertRaises(ValidationError):
            change_review_status(user=self.user, review_id=review.pk, new_status=Review.Status.ACCEPTED)
        with self.assertRaises(ValidationError):
            perform_bulk_review_action(user=self.user, review_ids=[review.pk], action='change_status', new_status=Review.Status.ACCEPTED)
        review.refresh_from_db()
        self.assertEqual(review.status, Review.Status.REJECTED)

    def test_import_rechecks_without_reparsing(self):
        form = ReviewBulkImportForm({'anthology': self.book.pk,
            'records': 'JAN TESTOWY;Nowy tytuł;fantasy;1000;;jan@example.com;'}, user=self.user)
        self.assertEqual(import_reviews(user=self.user, form=form), 1)
        self.assertEqual(Review.objects.get().title, 'Nowy tytuł')
        with self.assertRaises(ValidationError):
            import_reviews(user=self.user, form=form)
        self.assertEqual(Review.objects.count(), 1)

    def test_activity_description_needs_no_database(self):
        from types import SimpleNamespace
        match = SimpleNamespace(url_name='assigned_text_detail', kwargs={'text_id': 77}, namespace='core')
        with self.assertNumQueries(0):
            self.assertIn('#77', describe_request(match, 'GET')[1])

    def test_activity_throttle_and_write_failure(self):
        middleware = UserActivityMiddleware(lambda r: HttpResponse())
        request = self.factory.get('/texts/')
        request.user = self.user
        request.session = {}
        request._cms_activity = ('Wszystkie teksty', '')
        middleware(request)
        middleware(request)
        call_command('flush_activity', stdout=StringIO())
        self.assertEqual(UserActivity.objects.count(), 1)
        request.method = 'POST'
        middleware(request)
        call_command('flush_activity', stdout=StringIO())
        self.assertEqual(UserActivity.objects.count(), 2)
        with patch('core.activity_spool.enqueue_activity', side_effect=RuntimeError('test')), patch('core.activity.logger.exception'):
            self.assertEqual(middleware(request).status_code, 200)

    def test_retention_preserves_mutations_and_last_seen(self):
        old = timezone.now() - timedelta(days=300)
        visit = UserActivity.objects.create(user=self.user, actor='x', method='GET', action='Pulpit', target='', path='/', status_code=200)
        mutation = UserActivity.objects.create(user=self.user, actor='x', method='POST', action='Zapis', target='', path='/', status_code=200)
        latest = UserActivity.objects.create(user=self.user, actor='x', method='GET', action='Pulpit', target='', path='/', status_code=200)
        UserActivity.objects.all().update(created_at=old)
        call_command('prune_activity', days=180, dry_run=True, stdout=StringIO())
        self.assertEqual(UserActivity.objects.count(), 3)
        call_command('prune_activity', days=180, stdout=StringIO())
        self.assertFalse(UserActivity.objects.filter(pk=visit.pk).exists())
        self.assertTrue(UserActivity.objects.filter(pk=mutation.pk).exists())
        self.assertTrue(UserActivity.objects.filter(pk=latest.pk).exists())

    def test_prefill_private_authorized_and_expiring(self):
        self.client.force_login(self.user)
        url = reverse('admin:texts_review_prepare_text')
        review=self.review(status=Review.Status.ACCEPTED,author_notified_at=timezone.localdate())
        Author.objects.filter(email__iexact=review.email).update(has_contract=True)
        if not Author.objects.filter(email__iexact=review.email).exists():
            Author.objects.create(first_name='Jan',last_name='Testowy',email=review.email,has_contract=True)
        data = {'review_id': review.pk, 'title':'Nowy', 'source_author_first_name':'Jan', 'source_author_last_name':'Testowy',
                'source_author_email':'private@example.com', 'length':1000, 'anthology':self.book.pk}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        popup = response.json()['url']
        self.assertNotIn('private', popup)
        self.assertNotIn('Nowy', popup)
        response = self.client.get(popup)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'private@example.com')
        self.client.logout()
        self.assertNotEqual(self.client.post(url, data).status_code, 200)
        other = get_user_model().objects.create_superuser('other', 'other@example.com', 'unused')
        self.client.force_login(other)
        self.assertEqual(self.client.get(popup).status_code, 400)

    def test_admin_renders_prefill_endpoint(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('admin:texts_review_add'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-prepare-text-url')

    def test_coauthor_warning_requires_confirmation(self):
        from texts.admin import ReviewAdminForm
        coauthor = Author.objects.create(first_name='Anna', last_name='Test', email='anna@example.com', is_blacklisted=True)
        data = {'author': self.author.pk, 'coauthors': [coauthor.pk], 'title': 'Test', 'length': 1000,
                'anthology': self.book.pk, 'status': Review.Status.NEW}
        form = ReviewAdminForm(data)
        self.assertFalse(form.is_valid())
        self.assertTrue(any('Współautor' in warning for warning in form.submission_warnings))

    def test_csrf_and_non_superuser_cannot_prepare_popup(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse('admin:texts_review_prepare_text'), {}).status_code, 403)
        member = get_user_model().objects.create_user('member', 'member@example.com', is_staff=True)
        self.client.force_login(member)
        self.assertIn(self.client.post(reverse('admin:texts_review_prepare_text'), {}).status_code, (302, 403))

    def test_expired_prefill_is_not_loaded(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin:texts_review_prepare_text'), {'review_id': self.review().pk, 'title': 'EXPIRED PRIVATE VALUE'})
        session = self.client.session
        entries = session['review_text_prefills']
        for entry in entries.values():
            entry['at'] = 0
        session['review_text_prefills'] = entries
        session.save()
        self.assertContains(self.client.get(response.json()['url']), 'wygasły', status_code=400)

    def test_pages_render_with_collected_manifest(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as static_root:
            with self.settings(DEBUG=False, STATIC_ROOT=static_root, STORAGES={
                'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'},
            }):
                call_command('collectstatic', interactive=False, verbosity=0)
                self.client.force_login(self.user)
                for url in [reverse('core:programs'), reverse('admin:texts_review_add'), reverse('admin:texts_text_add')]:
                    self.assertEqual(self.client.get(url).status_code, 200)
