from datetime import timedelta
from django.core.exceptions import ValidationError
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from authors.models import Author
from core.admin import AccountCreationForm
from core.forms import AuthorNotificationForm, StartStageForm
from core.selectors.reviews import review_list_context
from core.selectors.texts import workflow_list_context
from core.services.reviews import save_author_notification
from core.tests import CoreTestDataMixin
from texts.blacklist import apply_blacklist
from texts.models import Review


class ReleasePatchTests(CoreTestDataMixin, TestCase):
    def hidden_review(self):
        author = Author.objects.create(first_name="Jan", last_name="Nowak", email="black@example.com", is_blacklisted=True)
        review = Review(author=author, author_first_name="Jan", author_last_name="Nowak", email=author.email,
                        title="Tajne zgłoszenie", genre="fantasy", length=2500, anthology=self.anthology)
        apply_blacklist(review)
        review.full_clean()
        review.save()
        return review

    def test_notification_initial_none_and_date_limits(self):
        today = timezone.localdate()
        self.assertEqual(AuthorNotificationForm(initial={"author_notified_at": None}).initial['author_notified_at'], today)
        self.assertEqual(StartStageForm().initial['started_at'], today)
        for days in (-15, -14, 14, 15):
            form = AuthorNotificationForm({'author_notified': True, 'author_notified_at': today + timedelta(days=days)})
            self.assertEqual(form.is_valid(), abs(days) <= 14)

    def test_blacklist_dates_and_visibility(self):
        review = self.hidden_review()
        self.assertTrue(review.is_hidden)
        self.assertEqual(review.status, Review.Status.REJECTED)
        self.assertTrue(14 <= (review.decision_at - timezone.localdate()).days <= 28)
        self.assertFalse(Review.objects.awaiting_notification().filter(pk=review.pk).exists())
        for user in (self.reviewer, self.coordinator):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('core:assigned_review_detail', args=[review.pk])).status_code, 404)
            data = review_list_context(user=user, params=QueryDict('filters_applied=1'))
            self.assertNotIn(review.pk, [r['pk'] for r in data['reviews']])
        self.client.force_login(self.superuser)
        self.assertContains(self.client.get(reverse('core:assigned_review_detail', args=[review.pk])), 'odrzucenie zaplanowane')

    def test_hidden_notification_becomes_due_without_cron(self):
        review = self.hidden_review()
        with self.assertRaises(ValidationError):
            save_author_notification(user=self.superuser, review_id=review.pk, author_notified_at=timezone.localdate())
        review.decision_at = timezone.localdate()
        review.save(update_fields=['decision_at'])
        self.assertTrue(Review.objects.awaiting_notification().filter(pk=review.pk).exists())
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('core:assigned_review_detail', args=[review.pk]))
        self.assertTrue(response.context['can_notify_author'])
        self.assertFalse(response.context['can_copy_review_to_text'])
        save_author_notification(user=self.superuser, review_id=review.pk, author_notified_at=timezone.localdate())
        self.assertFalse(Review.objects.awaiting_notification().filter(pk=review.pk).exists())

    def test_account_requires_names_and_accepts_spaces(self):
        data = {'username': 'Moje konto 123', 'password1': 'Long-Secret-928461!', 'password2': 'Long-Secret-928461!'}
        self.assertFalse(AccountCreationForm(data).is_valid())
        form = AccountCreationForm({**data, 'first_name': 'Jan', 'last_name': 'Kowalski'})
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.username, 'Moje konto 123')

    def test_workflow_columns_follow_filters(self):
        data = workflow_list_context(user=self.superuser, params=QueryDict('stage=editing&stage=first_proofreading'))
        self.assertEqual({value for value, label in data['role_columns']}, {'editor', 'proofreader_1'})
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('core:workflow_list'), {'stage': ['editing', 'first_proofreading']})
        self.assertNotContains(response, '>Cykl<')

    def test_polish_routes_and_theme(self):
        self.assertEqual(reverse('login'), '/konto/logowanie/')
        self.assertEqual(reverse('admin:people_person_changelist'), '/panel/zespol/osoba/')
        self.assertEqual(reverse('illustrations:cover_proposal_list'), '/ilustracje/propozycje-okladek/')
        self.assertContains(self.client.get(reverse('login')), 'data-theme-toggle')
        self.client.force_login(self.superuser)
        self.assertNotContains(self.client.get(reverse('admin:index')), 'data-theme-toggle')
