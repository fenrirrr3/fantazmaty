"""Powiązanie zgłoszenia z tekstem (źródło): wyszukiwanie, potwierdzenia, odpięcie i lista niepowiązanych."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.services.reviews import copy_review_to_text
from core.source_reviews import link_source_review, linkable_reviews, validate_source_review_link
from fantazmaty.testing import create_member, create_superuser, edit_token, form_value
from texts.models import Anthology, Review, ReviewAssignment, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage


class SourceReviewLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.manager = create_member('manager', 'Koordynator redakcji')
        cls.reader = create_member('reader', 'Recenzent')
        editor = create_member('editor', 'Redaktor')
        cls.book = Anthology.objects.create(title='Testowa antologia')
        cls.original = Anthology.objects.create(title='Fantazmaty 4', status='ready')
        cls.author = Author.objects.create(first_name='Ala', last_name='Autorka', email='author@example.test')
        cls.text = Text.objects.create(title='Opowiadanie przeniesione', anthology=cls.book, length=100)
        cls.text.authors.add(cls.author)
        assignment = WorkflowRoleAssignment.objects.create(text=cls.text, role='editor', assigned_to=editor)
        WorkflowStage.objects.create(text=cls.text, stage_type='editing', assignment=assignment,
                                     started_at=timezone.localdate())

    def setUp(self):
        self.client.force_login(self.admin)

    def review(self, **values):
        values.setdefault('anthology', self.book)
        return Review.objects.create(title=self.text.title, author=self.author, length=100, **values)

    def test_archived_submission_links_only_with_confirmation_and_keeps_decision(self):
        archive = self.review(old_reviews=True, status='new')
        self.assertIn(archive, linkable_reviews(self.text))
        with self.assertRaises(ValidationError):
            link_source_review(user=self.admin, text_id=self.text.pk, review_id=archive.pk)
        link_source_review(user=self.admin, text_id=self.text.pk, review_id=archive.pk, confirm_mismatch=True)
        archive.refresh_from_db()
        self.assertEqual(archive.copied_text, self.text)
        self.assertEqual(archive.status, 'new')
        self.assertTrue(archive.old_reviews)
        response = self.client.get(reverse('core:link_text_review', args=[self.text.pk]), {'q': 'Opowiadanie'})
        self.assertContains(response, f'id="source-{archive.pk}"')
        # Zgłoszenie powiązane z jednym tekstem nie jest oferowane innemu.
        other = Text.objects.create(title='Inny tekst', anthology=self.book, length=10)
        self.assertNotIn(archive, linkable_reviews(other))
        with self.assertRaises(ValidationError):
            link_source_review(user=self.admin, text_id=other.pk, review_id=archive.pk, confirm_mismatch=True)

    def test_rejected_never_linkable_other_anthology_allowed_linked_protected(self):
        rejected = self.review(status='rejected', old_reviews=True, anthology=self.original)
        other = self.review(old_reviews=True, status='accepted', anthology=Anthology.objects.create(title='Inna'))
        linked = self.review(old_reviews=True, status='accepted',
                             copied_text=Text.objects.create(title='Inny', length=1))
        self.assertIn(other, linkable_reviews(self.text))
        validate_source_review_link(self.text, other, confirm_mismatch=True)
        # Odrzucone zgłoszenia nie są oferowane i nie dają się powiązać nawet z potwierdzeniem.
        self.assertNotIn(rejected, linkable_reviews(self.text))
        with self.assertRaises(ValidationError):
            validate_source_review_link(self.text, rejected, confirm_mismatch=True)
        for review in (rejected, linked):
            with self.subTest(review=review.status):
                self.assertNotIn(review, linkable_reviews(self.text))
                with self.assertRaises(ValidationError):
                    link_source_review(user=self.admin, text_id=self.text.pk, review_id=review.pk,
                                       confirm_mismatch=True)

    def test_cross_anthology_source_search_and_save_dont_move_original_submission(self):
        review = self.review(anthology=self.original, status='accepted', old_reviews=True)
        url = reverse('core:link_text_review', args=[self.text.pk])
        response = self.client.get(url, {'q': 'przeniesione'})
        self.assertContains(response, f'id="source-{review.pk}"')
        self.assertContains(response, 'Antologia zgłoszenia')
        self.assertContains(response, self.original.title)
        saved = self.client.post(url, {'review_id': review.pk, '_edit_version': form_value(response)})
        self.assertEqual(saved.status_code, 302)
        review.refresh_from_db()
        self.text.refresh_from_db()
        self.assertEqual(review.copied_text_id, self.text.pk)
        self.assertEqual(review.anthology_id, self.original.pk)
        self.assertEqual(self.text.anthology_id, self.book.pk)
        self.assertEqual(review.status, 'accepted')
        self.assertTrue(review.old_reviews)

    def test_unlinked_list_permissions_and_exact_scope(self):
        ready = Anthology.objects.create(title='Gotowe', status='ready')
        Text.objects.create(title='Już wydany', anthology=ready, length=100)
        Text.objects.create(title='Bez antologii', length=100)
        linked = Text.objects.create(title='Powiązany', anthology=self.book, length=100)
        self.review(status='accepted', copied_text=linked)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('core:unlinked_reviews')).status_code, 403)
        self.assertNotContains(self.client.get(reverse('core:review_list')), 'Niepowiązane')
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('core:review_list')), 'Niepowiązane')
        page = self.client.get(reverse('core:unlinked_reviews'))
        self.assertEqual([t['pk'] for t in page.context['texts']], [self.text.pk])
        self.assertContains(page, 'Powiąż recenzje')
        page = self.client.get(reverse('core:unlinked_reviews'), {'q': 'Autorka'})
        self.assertEqual(page.context['page_obj'].paginator.count, 1)

    def test_link_button_disappears_only_for_linked_review(self):
        text = Text.objects.create(title='Tekst z recenzją', anthology=self.book, length=1)
        url = reverse('core:assigned_text_detail', args=[text.pk])
        self.assertContains(self.client.get(url), 'Powiąż zgłoszenie z recenzjami')
        review = Review.objects.create(title=text.title, anthology=self.book, length=1, copied_text=text)
        page = self.client.get(url)
        self.assertNotContains(page, 'Powiąż zgłoszenie z recenzjami')
        self.assertContains(page, reverse('core:assigned_review_detail', args=[review.pk]))

    def test_unlink_requires_confirmation_preserves_history_and_prevents_duplicate(self):
        # Tekst bez autorów: strona powiązania nie ma wtedy podpowiedzi zgłoszeń.
        text = Text.objects.create(title='Testowy tekst', anthology=self.book, length=100)
        review = Review.objects.create(title=text.title, anthology=self.book, length=100,
                                       status='accepted', copied_text=text)
        opinion = ReviewAssignment.objects.create(review=review, user=self.reader, position=1, opinion='yes',
                                                  notes='Opinia')
        url = reverse('core:link_text_review', args=[text.pk])
        data = {'action': 'unlink', 'review_id': review.pk, '_edit_version': edit_token(self.admin, text)}
        self.client.post(url, data)
        review.refresh_from_db()
        self.assertEqual(review.copied_text_id, text.pk)
        response = self.client.post(url, {**data, 'confirm_unlink': 'on'})
        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertIsNone(review.copied_text_id)
        self.assertTrue(review.publication_detached)
        self.assertEqual(review.status, 'accepted')
        self.assertTrue(ReviewAssignment.objects.filter(pk=opinion.pk, notes='Opinia').exists())
        self.assertEqual(self.client.post(url, {**data, 'confirm_unlink': 'on'}).status_code, 409)
        link_source_review(user=self.admin, text_id=text.pk, review_id=review.pk, confirm_mismatch=True)
        review.refresh_from_db()
        self.assertFalse(review.publication_detached)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_replacing_source_detaches_previous_submission_from_publication(self):
        """Chroni przed ponownym importem poprzedniego zgłoszenia jako duplikatu po podmianie źródła."""
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor@example.com',
                                       has_contract=True)
        text = Text.objects.create(title='Tytuł', length=100, anthology=self.book)
        text.authors.add(author)
        common = dict(anthology=self.book, length=100, genre='fantasy', status='accepted',
                      author_notified_at=timezone.localdate())
        old = Review.objects.create(title='Tytuł', author=author, author_first_name='Jan',
                                    author_last_name='Autor', email='autor@example.com', **common)
        new = Review.objects.create(title='Inny tytuł', author_first_name='Inna', author_last_name='Osoba',
                                    email='inna@example.com', **common)
        link_source_review(user=self.admin, text_id=text.pk, review_id=old.pk)
        with self.assertRaises(ValidationError):
            link_source_review(user=self.admin, text_id=text.pk, review_id=new.pk)
        old.refresh_from_db()
        self.assertEqual(old.copied_text_id, text.pk)
        link_source_review(user=self.admin, text_id=text.pk, review_id=new.pk, confirm_mismatch=True)
        old.refresh_from_db()
        self.assertIsNone(old.copied_text_id)
        self.assertTrue(old.publication_detached)
        with self.assertRaises(ValidationError):
            copy_review_to_text(user=self.admin, review_id=old.pk)
        response = self.client.get(reverse('core:home'))
        self.assertNotIn(old.pk, [r.pk for r in response.context['pending_publication_reviews']])
        link_source_review(user=self.admin, text_id=text.pk, review_id=old.pk)
        old.refresh_from_db()
        self.assertFalse(old.publication_detached)
