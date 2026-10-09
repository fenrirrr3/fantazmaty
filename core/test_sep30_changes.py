from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from texts.models import Anthology, Review, Text
from workflow.models import WorkflowStage
from core.services.reviews import copy_review_to_text
from core.views.search import _search_reviews
from core.selectors.texts import workflow_list_context
from core.intake_forms import SingleReviewForm
from texts.admin import ReviewAdminForm

class ChangesTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_superuser('admin','admin@example.com','test')
        self.book=Anthology.objects.create(title='Nabór')
        self.review=Review.objects.create(title='Archiwalny test',anthology=self.book,author_first_name='Jan',author_last_name='Test',email='jan@example.com',author_pseudonym='Pseudonim',length=100,old_reviews=True,status='accepted',author_notified_at=timezone.localdate())
    def test_archive_search_privacy(self):
        self.assertEqual(len(_search_reviews('Archiwalny',include_authors=False)),1)
        self.assertNotIn('email',_search_reviews('Archiwalny',include_authors=False)[0])
        self.assertEqual(_search_reviews('Archiwalny',include_authors=False,include_archived=False),[])
    def test_transfer_archive_idempotent(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('core:assigned_review_detail',args=[self.review.pk])),'Dodaj do procesu wydawniczego')
        text=copy_review_to_text(user=self.user,review_id=self.review.pk,contract_received=True)
        self.assertEqual(text.authors.get().pseudonym,'Pseudonim')
        self.assertEqual(copy_review_to_text(user=self.user,review_id=self.review.pk).pk,text.pk)
        self.review.refresh_from_db()
        self.assertTrue(self.review.old_reviews)

    def test_dashboard(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("core:home")), self.review.title)
        copy_review_to_text(user=self.user, review_id=self.review.pk, contract_received=True)
        self.assertNotContains(self.client.get(reverse("core:home")), self.review.title)

    def test_hide_ready(self):
        text = Text.objects.create(title="Gotowy", length=100)
        WorkflowStage.objects.create(text=text, stage_type="ready")
        self.assertEqual(
            len(list(workflow_list_context(user=self.user, params={"hide_ready": "1"})["stages"])),
            0,
        )
        self.assertEqual(
            len(list(workflow_list_context(user=self.user, params={"hide_ready": "0"})["stages"])),
            1,
        )

    def test_pseudonym_form(self):
        for cls in [SingleReviewForm, ReviewAdminForm]:
            form = cls(
                data={
                    "author_first_name": "Adam",
                    "author_last_name": "Nowy",
                    "author_pseudonym": "Pióro",
                    "title": "Nowy",
                    "email": "adam@example.com",
                    "anthology": self.book.pk,
                    "length": 100,
                    "genre": "fantasy",
                    "status": "new",
                }
            )
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.save(commit=False).author_pseudonym, "Pióro")
