from django.test import TestCase
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.urls import reverse
from workflow.tests import WorkflowTestDataMixin
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import claim_ready_for_editing, claim_stage, resume_editing
from workflow.handoffs import handoff_stage
from core.services.texts import start_assigned_stage
from core.services.reviews import copy_review_to_text
from texts.models import Text, Anthology, Review
from authors.models import Author

class WorkflowGatesTests(WorkflowTestDataMixin, TestCase):
    def setUp(self):
        self.book=Anthology.objects.create(title='Test')
        self.text=Text.objects.create(title='Opowiadanie',length=1000,anthology=self.book)
        self.today=timezone.localdate()

    def completed(self,kind,**kwargs):
        from workflow.import_context import importing_completed
        token=importing_completed.set(True)
        try:
            return S.objects.create(text=self.text,stage_type=kind,is_completed=True,imported_completed=True,**kwargs)
        finally:importing_completed.reset(token)

    def test_imported_editing_allows_verifier(self):
        self.completed('editing')
        S.objects.create(text=self.text,stage_type='first_verification')
        claim_stage(self.text,'first_verification',self.verifier_1)
        self.assertTrue(A.objects.filter(text=self.text,assigned_to=self.verifier_1).exists())

    def test_handoff_editing_can_start_with_prepared_verification(self):
        S.objects.create(text=self.text,stage_type='ready_for_editing')
        claim_ready_for_editing(self.text,self.editor)
        stage=S.objects.get(text=self.text,stage_type='editing')
        handoff_stage(self.text,self.superuser,stage_id=stage.pk,assigned_to_id=self.other_editor.pk,expected_assignment_id=stage.assignment_id,reason='Zmiana')
        start_assigned_stage(user=self.other_editor,stage_id=stage.pk,started_at=self.today)
        stage.refresh_from_db();self.assertEqual(stage.started_at,self.today)

    def test_started_verification_blocks_editing_start(self):
        assignment=A.objects.create(text=self.text,role='editor',assigned_to=self.editor)
        stage=S.objects.create(text=self.text,stage_type='editing',assignment=assignment)
        S.objects.create(text=self.text,stage_type='first_verification',started_at=self.today)
        with self.assertRaises(ValidationError):
            start_assigned_stage(user=self.editor,stage_id=stage.pk,started_at=self.today)

    def test_handoff_author_wait_preserves_date_and_resume_closes_wait(self):
        assignment=A.objects.create(text=self.text,role='editor',assigned_to=self.editor)
        self.completed('editing',assignment=assignment);self.completed('first_verification')
        stage=S.objects.create(text=self.text,stage_type='author_editing',assignment=assignment,started_at=self.today)
        handoff_stage(self.text,self.superuser,stage_id=stage.pk,assigned_to_id=self.other_editor.pk,expected_assignment_id=assignment.pk,reason='Zmiana')
        stage.refresh_from_db();self.assertEqual(stage.started_at,self.today)
        resume_editing(self.text,self.other_editor)
        stage.refresh_from_db();self.assertTrue(stage.is_completed)
        self.assertEqual(list(S.objects.filter(text=self.text,is_completed=False).values_list('stage_type',flat=True)),['editing'])

    def test_damaged_author_wait_does_not_create_parallel_editing(self):
        A.objects.create(text=self.text,role='editor',assigned_to=self.editor)
        self.completed('first_verification')
        S.objects.create(text=self.text,stage_type='author_editing')
        with self.assertRaises(ValidationError):resume_editing(self.text,self.editor)
        self.assertFalse(S.objects.filter(text=self.text,stage_type='editing').exists())

    def review(self,**kwargs):
        author=Author.objects.create(first_name='Jan',last_name='Autor',email='jan@example.com',has_contract=True)
        data=dict(title='Recenzja',length=1000,genre='fantasy',email=author.email,author=author,author_first_name='Jan',author_last_name='Autor',anthology=self.book,status=Review.Status.ACCEPTED,author_notified_at=timezone.now())
        data.update(kwargs)
        return Review.objects.create(**data)

    def test_ready_anthology_copy_rejected_without_creation(self):
        ready=Anthology.objects.create(title='Ready',status='ready')
        review=self.review(anthology=ready)
        with self.assertRaises(ValidationError):copy_review_to_text(user=self.superuser,review_id=review.pk)
        review.refresh_from_db();self.assertIsNone(review.copied_text_id)
        self.assertEqual(Text.objects.count(),1)

    def test_admin_popup_cannot_bypass_acceptance(self):
        review=self.review(status=Review.Status.NEW)
        self.client.force_login(self.superuser)
        url=self.client.post(reverse('admin:texts_review_prepare_text'),{'review_id':review.pk,'title':'New'}).json()['url']
        self.assertEqual(self.client.get(url).status_code,400)
        self.assertEqual(self.client.post(url,{'title':'New'}).status_code,400)
        self.assertEqual(Text.objects.count(),1)

    def test_ready_transition_rejects_pending_and_missing_workflow(self):
        self.book.status='ready'
        with self.assertRaises(ValidationError):self.book.save()
        self.book.refresh_from_db();self.assertEqual(self.book.status,'in_preparation')
        S.objects.create(text=self.text,stage_type='ready')
        self.book.status='ready';self.book.save()
        self.book.refresh_from_db();self.assertEqual(self.book.status,'ready')

    def test_popup_without_notification_or_contract_is_blocked(self):
        review=self.review(author_notified_at=None)
        self.client.force_login(self.superuser)
        url=self.client.post(reverse('admin:texts_review_prepare_text'),{'review_id':review.pk}).json()['url']
        self.assertEqual(self.client.get(url).status_code,400)
        review.author_notified_at=self.today;review.save()
        review.author.has_contract=False;review.author.save()
        self.assertEqual(self.client.get(url).status_code,400)

    def test_popup_changed_author_without_contract_rolls_back(self):
        review=self.review()
        self.client.force_login(self.superuser)
        other=Author.objects.create(first_name='Inny',last_name='Autor',email='other@example.com')
        url=self.client.post(reverse('admin:texts_review_prepare_text'),{
            'review_id':review.pk,'title':'Nowy','length':1000,'anthology':self.book.pk,
            'authors':[other.pk],
        }).json()['url']
        response=self.client.get(url)
        form=response.context['adminform'].form
        data={name:form[name].value() for name in form.fields if form[name].value() is not None}
        data.update({'_popup':'1','_save':'Save'})
        for inline in response.context['inline_admin_formsets']:
            for field in inline.formset.management_form:data[field.html_name]=field.value()
        self.assertEqual(self.client.post(url,data).status_code,400)
        self.assertEqual(Text.objects.count(),1)
        review.refresh_from_db();self.assertIsNone(review.copied_text_id)

    def test_withdrawn_text_does_not_block_ready_anthology(self):
        S.objects.create(text=self.text,stage_type='withdrawn')
        self.book.status='ready';self.book.full_clean();self.book.save()

    def test_ready_marker_does_not_hide_pending_repeat(self):
        S.objects.create(text=self.text,stage_type='ready')
        S.objects.create(text=self.text,stage_type='editing',is_released=False)
        self.book.status='ready'
        with self.assertRaises(ValidationError):self.book.full_clean()
