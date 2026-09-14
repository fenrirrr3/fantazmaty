from core.testing_forms import post_form
from datetime import timedelta
import re
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin, create_member
from core.models import UserActivity
from core.intake_forms import SingleReviewForm
from core.author_contact import stored_author_phone
from core.search_people import rank_people
from people.models import Person
from texts.models import Text, TextNote, Review, ReviewAssignment
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.services import create_pending_stage


class Revision7Tests(CoreTestDataMixin, TestCase):
    def setUp(self):
        self.text = Text.objects.create(title='Nowy tekst', anthology=self.anthology, length=100)
        self.text.authors.add(self.author)
        self.client.force_login(self.editor)

    def review(self, **kwargs):
        return Review.objects.create(title=kwargs.pop('title', 'Zgłoszenie'), anthology=self.anthology,
            author=self.author, author_first_name=self.author.first_name, author_last_name=self.author.last_name,
            email=self.author.email, genre='Fantasy', length=100, **kwargs)

    def assign(self, **kwargs):
        data = dict(text=self.text, role='editor', assigned_to=self.editor, workflow_cycle=1)
        data.update(kwargs)
        return WorkflowRoleAssignment.objects.create(**data)

    def test_author_email_only_assigned_text_and_no_source_author_leak(self):
        review = self.review(copied_text=self.text)
        url = reverse('core:assigned_text_detail', args=[self.text.pk])
        self.assertNotContains(self.client.get(url), self.author.email)
        self.assign()
        response = self.client.get(url)
        self.assertContains(response, self.author.email)
        self.assertNotContains(response, self.author.last_name)
        other = Text.objects.create(title='Inny', length=100)
        other.authors.add(self.author)
        self.assertNotContains(self.client.get(reverse('core:assigned_text_detail', args=[other.pk])), self.author.email)
        self.assertNotContains(self.client.get(reverse('core:assigned_review_detail', args=[review.pk])), self.author.email)

    def test_past_cycle_assignment_keeps_email_access(self):
        self.assign()
        self.text.current_workflow_cycle = 2
        self.text.save(update_fields=['current_workflow_cycle'])
        self.assertContains(self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk])), self.author.email)

    def test_note_owner_can_edit_delete_without_current_assignment(self):
        note = TextNote.objects.create(text=self.text, author=self.editor, content='Stara')
        url = reverse('core:edit_text_note', args=[self.text.pk, note.pk])
        response = post_form(self.client, url, {'content':'Nowa', 'is_important':'on'})
        self.assertEqual(response.status_code, 302)
        note.refresh_from_db()
        self.assertEqual(note.content, 'Nowa')
        self.assertTrue(note.is_important)
        self.assertEqual(note.author, self.editor)
        response = post_form(self.client, reverse('core:delete_text_note', args=[self.text.pk, note.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TextNote.objects.filter(pk=note.pk).exists())

    def test_note_other_member_forbidden_and_get_cannot_delete(self):
        note = TextNote.objects.create(text=self.text, author=self.reviewer, content='Nie zmieniaj')
        self.assign()
        for route in ['edit_text_note', 'delete_text_note']:
            response = post_form(self.client, reverse('core:'+route, args=[self.text.pk, note.pk]), {'content':'Atak'})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(reverse('core:delete_text_note', args=[self.text.pk, note.pk])).status_code, 405)
        note.refresh_from_db()
        self.assertEqual(note.content, 'Nie zmieniaj')

    def test_note_coordinator_can_manage_and_wrong_text_rejected(self):
        note = TextNote.objects.create(text=self.text, author=self.reviewer, content='Stara')
        self.client.force_login(self.coordinator)
        other = Text.objects.create(title='Inny', length=100)
        self.assertEqual(post_form(self.client, reverse('core:delete_text_note', args=[other.pk, note.pk])).status_code, 404)
        self.assertEqual(post_form(self.client, reverse('core:edit_text_note', args=[self.text.pk, note.pk]), {'content':'Poprawiona'}).status_code, 302)
        note.refresh_from_db()
        self.assertEqual(note.author, self.reviewer)
        self.assertEqual(note.content, 'Poprawiona')

    def test_note_stale_form_is_rejected(self):
        note = TextNote.objects.create(text=self.text, author=self.editor, content='Stara')
        url = reverse('core:edit_text_note', args=[self.text.pk, note.pk])
        response = self.client.get(url)
        token = re.search(r'name="_edit_version" value="([^"]+)"', response.content.decode()).group(1)
        note.content = 'Inna edycja'
        note.save()
        response = post_form(self.client, url, {'content':'Nadpisana', '_edit_version':token})
        self.assertEqual(response.status_code, 409)
        note.refresh_from_db()
        self.assertEqual(note.content, 'Inna edycja')

    def test_team_email_visible_without_dropbox_or_details_column(self):
        self.editor_person.dropbox_email = 'private-dropbox@example.com'
        self.editor_person.save()
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('core:people_list'))
        self.assertContains(response, self.editor_person.email)
        self.assertNotContains(response, self.editor_person.dropbox_email)
        self.assertNotContains(response, '>Szczegóły</th>')
        self.assertContains(response, 'data-double-copy')
        response = self.client.get(reverse('core:person_detail', args=[self.editor_person.pk]))
        self.assertContains(response, self.editor_person.email)

    def test_people_names_before_email_no_role_or_dropbox_matches(self):
        first = Person.objects.create(first_name='Żaneta', last_name='Zeta', email='name@example.com')
        second = Person.objects.create(first_name='Anna', last_name='Alpha', email='zaneta@example.com')
        third = Person.objects.create(first_name='Inna', last_name='Osoba', email='x@example.com', dropbox_email='zaneta@dropbox.com')
        self.assertEqual(list(rank_people(Person.objects.all(), 'zaneta')), [first,second])
        self.client.force_login(self.superuser)
        for route in ['people_list', 'global_search']:
            response = self.client.get(reverse('core:'+route), {'q':'zaneta'})
            content = response.content.decode()
            self.assertLess(content.index('Żaneta'), content.index('Anna'))
            self.assertNotIn('Inna', content)

    def test_navigation_links_own_profile(self):
        response = self.client.get(reverse('core:home'))
        self.assertContains(response, f'href="{reverse("core:person_detail", args=[self.editor_person.pk])}"')
        self.assertNotContains(response, 'Osoby w zespole')

    def test_own_archived_votes_and_notes_only_in_archive(self):
        review = self.review(old_reviews=True, title='Moje stare zgłoszenie')
        ReviewAssignment.objects.create(review=review, historical_person=self.reviewer_person, position=1, opinion='no', notes='Moja stara opinia')
        ReviewAssignment.objects.create(review=review, user=self.editor, position=2, opinion='yes', notes='Cudza tajna opinia')
        self.client.force_login(self.reviewer)
        url = reverse('core:my_reviews')
        response = self.client.get(url, {'view':'archived', 'q':'stare'})
        self.assertContains(response, 'Moja stara opinia')
        self.assertNotContains(response, 'Cudza tajna opinia')
        self.assertNotContains(response, self.author.email)
        self.assertNotContains(response, reverse('core:assigned_review_detail', args=[review.pk]))
        self.assertNotContains(self.client.get(url, {'view':'all'}), review.title)
        self.assertEqual(self.client.get(reverse('core:assigned_review_detail', args=[review.pk])).status_code, 404)

    def test_team_member_without_reviewer_role_can_read_own_archive(self):
        ReviewAssignment.objects.create(review=self.review(old_reviews=True), historical_person=self.editor_person, position=1, opinion='maybe', notes='Historia')
        response = self.client.get(reverse('core:my_reviews'), {'view':'archived'})
        self.assertContains(response, 'Historia')
        self.assertContains(self.client.get(reverse('core:home')), 'Moje recenzje')

    def test_author_phone_uses_profile_not_owned_submission(self):
        self.author.phone_number = "444555666"
        self.author.save(update_fields=["phone_number"])
        self.review(phone_number='111222333')
        self.review(phone_number='444555666')
        self.review(phone_number='')
        self.assertEqual(stored_author_phone(self.author), '444555666')
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('core:author_suggestions'), {'q':self.author.email})
        self.assertEqual(response.json()['results'][0]['phone_number'], '444555666')
        form = SingleReviewForm(data={'author':self.author.pk,'title':'Nowy','genre':'Fantasy','length':100,'anthology':self.anthology.pk})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['phone_number'], '444555666')

    def test_claim_redirects_to_detail(self):
        stage = create_pending_stage(self.text, WorkflowStage.StageType.READY_FOR_EDITING)
        response = post_form(self.client, reverse('core:take_workflow_stage', args=[stage.pk]), {'started_at':timezone.localdate().isoformat()})
        self.assertRedirects(response, reverse('core:assigned_text_detail', args=[self.text.pk]))

    def test_complete_uses_today_without_calendar(self):
        proofreader, _ = create_member('korektor7', 'Korektor')
        self.assign(role='proofreader_1', assigned_to=proofreader)
        stage = WorkflowStage.objects.create(text=self.text, stage_type='first_proofreading', started_at=timezone.localdate()-timedelta(days=1))
        self.client.force_login(proofreader)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertNotContains(response, f'id="end-stage-{stage.pk}"')
        response = post_form(self.client, reverse('core:complete_workflow_stage', args=[stage.pk]))
        self.assertEqual(response.status_code, 302)
        stage.refresh_from_db()
        self.assertTrue(stage.is_completed)
        self.assertEqual(stage.ended_at, timezone.localdate())

    def test_activity_only_superuser_and_filters(self):
        self.client.get(reverse('core:people_list'))
        self.assertTrue(UserActivity.objects.filter(user=self.editor, action='Zespół').exists())
        url = reverse('core:user_activity')
        for user in (self.editor, self.coordinator):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(url, {'q':self.editor.get_username()})
        self.assertTrue(all(row.user_id==self.editor.pk for row in response.context['activities']))
        self.assertEqual(self.client.get(url, {'date_from':'nie-data'}).status_code, 400)

    def test_activity_excludes_payload_queries_and_captures_logout(self):
        self.client.get(reverse('core:people_list'), {'q':'secret-search'})
        note = TextNote.objects.create(text=self.text, author=self.editor, content='Original')
        post_form(self.client, reverse('core:edit_text_note', args=[self.text.pk,note.pk]), {'content':'secret-note'})
        post_form(self.client, reverse('logout'))
        rows = list(UserActivity.objects.filter(user=self.editor).values())
        self.assertNotIn('secret-note', str(rows))
        self.assertNotIn('secret-search', str(rows))
        self.assertTrue(any(row['action']=='Wylogowanie' for row in rows))
