from datetime import timedelta
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin, create_member
from core.testing_forms import post_form
from core.forms import VacationForm
from core.services.vacations import create_vacation, update_vacation
from core.services.texts import change_scheduled_stage, start_assigned_stage
from core.services.reviews import change_review_status, assign_reviewer, save_review_content_warnings
from core.selectors.texts import available_stages_for_user, my_texts_context, text_detail_context, workflow_list_context
from core.views.people import _profile_assignments
from core.pagination import paginate_items
from people.models import Vacation
from texts.models import Text, Review, ReviewAssignment
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.services import claim_ready_for_editing, claim_stage, send_to_first_verification, start_first_verification


class Update19Tests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.verifier, self.verifier_person = create_member('verify19', 'Weryfikator')

    def text(self):
        text = Text.objects.create(title='Test 19', anthology=self.anthology, length=1000)
        text.authors.add(self.author)
        S.objects.create(text=text, stage_type='ready_for_editing')
        return text

    def review(self, title='Review 19'):
        return Review.objects.create(title=title, author_first_name='Jan', author_last_name='Testowy', anthology=self.anthology, length=1000, genre='fantasy', email='test@example.com')

    def leave(self, person):
        return create_vacation(user=self.coordinator, person_id=person.pk, start_date=self.today, end_date=(self.today+timedelta(days=3)).isoformat())

    def test_editor_handoff_without_verifier_then_claim_and_start(self):
        text = self.text(); claim_ready_for_editing(text, self.editor)
        self.assertTrue(text_detail_context(user=self.editor,text=text)['can_send_to_first_verification'])
        stage = send_to_first_verification(text, self.editor)
        self.assertIsNone(stage.started_at)
        self.assertIn(stage.pk, [row['pk'] for row in available_stages_for_user(user=self.verifier)])
        claim_stage(text, 'first_verification', self.verifier)
        started = start_first_verification(text, self.verifier)
        self.assertEqual(started.started_at, self.today)

    def test_future_date_can_change_and_cancel_then_be_reclaimed(self):
        text=self.text(); stage=claim_ready_for_editing(text,self.editor,self.today+timedelta(days=4))
        change_scheduled_stage(user=self.editor,stage_id=stage.pk,started_at=self.today+timedelta(days=2))
        stage.refresh_from_db();self.assertEqual(stage.started_at,self.today+timedelta(days=2))
        change_scheduled_stage(user=self.editor,stage_id=stage.pk,cancel=True)
        self.assertFalse(A.objects.filter(text=text,assigned_to=self.editor).exists())
        claimed=claim_ready_for_editing(text,self.coordinator)
        self.assertEqual(claimed.started_at,self.today)

    def test_started_work_and_other_users_reservations_protected(self):
        text=self.text(); stage=claim_ready_for_editing(text,self.editor,self.today+timedelta(days=2))
        with self.assertRaises(PermissionDenied): change_scheduled_stage(user=self.verifier,stage_id=stage.pk,cancel=True)
        change_scheduled_stage(user=self.editor,stage_id=stage.pk,started_at=self.today)
        with self.assertRaises(ValidationError): change_scheduled_stage(user=self.editor,stage_id=stage.pk,cancel=True)

    def test_leave_blocks_claims_reviews_and_start(self):
        text=self.text(); self.leave(self.editor_person)
        with self.assertRaises(ValidationError): claim_ready_for_editing(text,self.editor)
        self.assertEqual(list(available_stages_for_user(user=self.editor)),[])
        self.leave(self.reviewer_person)
        with self.assertRaises(ValidationError): assign_reviewer(user=self.reviewer,review_id=self.review().pk)
        stage=S.objects.get(text=text);A.objects.create(text=text,role='editor',assigned_to=self.editor)
        with self.assertRaises(ValidationError): start_assigned_stage(user=self.superuser,stage_id=stage.pk,started_at=self.today)

    def test_overlap_validation_includes_contained_and_open_ended(self):
        first=self.leave(self.editor_person)
        with self.assertRaises(ValidationError):
            create_vacation(user=self.coordinator,person_id=self.editor_person.pk,start_date=self.today+timedelta(days=1),end_date=(self.today+timedelta(days=2)).isoformat())
        update_vacation(user=self.coordinator,vacation_id=first.pk,start_date=self.today,end_date=(self.today+timedelta(days=4)).isoformat())
        create_vacation(user=self.coordinator,person_id=self.editor_person.pk,start_date=self.today+timedelta(days=5),until_revoked=True)
        with self.assertRaises(ValidationError):
            create_vacation(user=self.coordinator,person_id=self.editor_person.pk,start_date=self.today+timedelta(days=6),until_revoked=True)
        self.assertEqual(Vacation.objects.filter(person=self.editor_person).count(),2)

    def test_coordinator_form_selects_person_and_default_today(self):
        form=VacationForm(instance=Vacation(person=self.editor_person))
        self.assertEqual(form['start_date'].value(),self.today)
        self.client.force_login(self.coordinator)
        url=reverse('core:my_vacations')
        response=self.client.get(url);self.assertContains(response,'name="person"')
        response=self.client.post(url,{'person':self.editor_person.pk,'start_date':self.today.isoformat(),'end_date':(self.today+timedelta(days=1)).isoformat()})
        self.assertEqual(response.status_code,302)
        self.assertTrue(Vacation.objects.filter(person=self.editor_person).exists())
        with self.assertRaises(PermissionDenied):
            create_vacation(user=self.reviewer,person_id=self.verifier_person.pk,start_date=self.today,until_revoked=True)

    def test_warning_case_lower_on_creation_preserved_on_edit(self):
        review=self.review();review.content_warnings='PTSD, Przemoc';review.save(update_fields=['content_warnings'])
        review.refresh_from_db();self.assertEqual(review.content_warnings,'PTSD, Przemoc')
        new=Review.objects.create(title='Nowy',anthology=self.anthology,length=100,content_warnings='PTSD, Przemoc')
        self.assertEqual(new.content_warnings,'ptsd, przemoc')
        text=self.text();text.content_warnings='PTSD';text.save(update_fields=['content_warnings']);text.refresh_from_db();self.assertEqual(text.content_warnings,'PTSD')

    def test_final_decision_removes_only_unfinished_assignments(self):
        for status in ('accepted','rejected'):
            review=self.review(status)
            ReviewAssignment.objects.create(review=review,user=self.reviewer,position=1,opinion='yes',notes='Ocena')
            ReviewAssignment.objects.create(review=review,user=self.editor,position=2,opinion='reading')
            ReviewAssignment.objects.create(review=review,user=self.verifier,position=3,opinion='reading')
            change_review_status(user=self.superuser,review_id=review.pk,new_status=status)
            self.assertEqual(list(review.assignments.values_list('opinion',flat=True)),['yes'])

    def test_my_reviews_opinion_filter_supports_multiple_choices(self):
        for title,opinion in [('Tak','yes'),('Nie','no'),('Może','maybe')]:
            ReviewAssignment.objects.create(review=self.review(title),user=self.reviewer,position=1,opinion=opinion)
        self.client.force_login(self.reviewer)
        response=self.client.get(reverse('core:my_reviews'),{'view':'completed','opinion':['yes','maybe']})
        self.assertEqual(response.context['page_obj'].paginator.count,2)
        self.assertContains(response,'name="opinion"')

    def test_profile_and_my_texts_agree_until_editor_control_done(self):
        text=self.text();claim_ready_for_editing(text,self.editor);send_to_first_verification(text,self.editor)
        self.assertEqual(_profile_assignments(self.editor_person,include_authors=False)[1]['completed'],0)
        self.assertEqual(len(my_texts_context(user=self.editor,selected_view='completed')['texts']),0)
        self.assertEqual(len(my_texts_context(user=self.editor,selected_view='waiting')['texts']),1)
        S.objects.filter(text=text).update(is_completed=True,started_at=self.today,ended_at=self.today)
        S.objects.create(text=text,stage_type='editor_control',is_completed=True,started_at=self.today,ended_at=self.today)
        self.assertEqual(_profile_assignments(self.editor_person,include_authors=False)[1]['completed'],1)
        self.assertEqual(len(my_texts_context(user=self.editor,selected_view='completed')['texts']),1)

    def test_sort_applies_before_pagination(self):
        for i in range(30): self.review(f'R{i:02}')
        request=RequestFactory().get('/',{'sort':'-title','page_size':25});request.user=self.superuser
        page=paginate_items(request,Review.objects.all())
        self.assertEqual(page[0].title,'R29');self.assertEqual(page[-1].title,'R05')
        self.assertEqual(page.sort_columns['Tytuł'],'title')

    def test_source_opinions_visible_in_text_detail(self):
        text=self.text();review=self.review();review.copied_text=text;review.save(update_fields=['copied_text'])
        ReviewAssignment.objects.create(review=review,user=self.reviewer,position=1,opinion='yes',notes='Unikalna recenzja źródłowa')
        self.client.force_login(self.editor)
        response=self.client.get(reverse('core:assigned_text_detail',args=[text.pk]))
        self.assertContains(response,'Unikalna recenzja źródłowa')
        self.assertContains(response,'class="review-summary-details" open')

    def test_default_workflow_sort_is_newest_assignment_within_stage(self):
        old=self.text();new=self.text()
        claim_ready_for_editing(old,self.editor);claim_ready_for_editing(new,self.editor)
        A.objects.filter(text=old).update(assigned_at=timezone.now()-timedelta(days=2))
        from django.http import QueryDict
        rows=list(workflow_list_context(user=self.superuser,params=QueryDict('stage=editing'))['stages'])
        edit_rows=[row for row in rows if row['stage_type']=='editing']
        self.assertEqual([row['text']['pk'] for row in edit_rows],[new.pk,old.pk])
