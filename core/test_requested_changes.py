from django.apps import apps
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.tests import CoreTestDataMixin, create_member
from core.models import Recruitment, AnthologyCorrection
from core.intake_forms import RecruitmentForm
from core.selectors.reviews import review_list_context
from core.selectors.texts import available_stages_for_user
from core.selectors.reports import _matches
from core.services.reviews import copy_review_to_text
from people.models import Role
from texts.models import Review, ReviewAssignment, Text
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.services import claim_stage


class RequestedChangesTests(CoreTestDataMixin, TestCase):
    def review(self, **kwargs):
        data = dict(title='ŻÓŁĆ i Źdźbło', anthology=self.anthology, length=1000,
                    genre='fantasy', author_first_name=self.author.first_name,
                    author_last_name=self.author.last_name, email=self.author.email)
        data.update(kwargs)
        return Review.objects.create(**data)

    def test_search_ignores_polish_accents_but_keeps_wildcards_literal(self):
        text = Text.objects.create(title='ŻÓŁĆ i Źdźbło 100%_koniec', length=1000)
        for query in ('zolc', 'ŻÓŁĆ', 'zDzBlo', 'ŹDŹBŁO', '100%_'):
            self.assertEqual(list(Text.objects.filter(title__plcontains=query)), [text])
        self.assertFalse(Text.objects.filter(title__plcontains='100%koniec').exists())
        self.assertTrue(_matches('zolc zdzblo', text.title))
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('core:global_search'), {'query':'zolc zdzblo'})
        self.assertContains(response, text.title)
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('core:global_search'), {'query':'PoufneImie'})
        self.assertNotContains(response, self.author.email)

    def test_styling_only_superuser_can_claim_or_be_assigned(self):
        text = Text.objects.create(title='Stylowanie', length=1000)
        stage = WorkflowStage.objects.create(text=text, stage_type='styling')
        self.assertIn(stage.pk, [row['pk'] for row in available_stages_for_user(user=self.superuser)])
        self.assertNotIn(stage.pk, [row['pk'] for row in available_stages_for_user(user=self.coordinator)])
        for user in (self.coordinator, self.editor, self.reviewer):
            with self.assertRaises(PermissionDenied):
                claim_stage(text, 'styling', user)
        with self.assertRaises(ValidationError):
            WorkflowRoleAssignment(text=text, role='styling', assigned_to=self.coordinator).full_clean()
        claimed = claim_stage(text, 'styling', self.superuser)
        self.assertIsNotNone(claimed.started_at)
        self.assertEqual(text.workflow_role_assignments.get(role='styling').assigned_to_id, self.superuser.pk)

    def test_existing_contract_without_link_is_shown_and_reused(self):
        self.author.has_contract = True
        self.author.save()
        review = self.review(status='accepted', author_notified_at=timezone.now())
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('core:assigned_review_detail', args=[review.pk]))
        self.assertTrue(response.context['author_has_contract'])
        self.assertNotContains(response, 'name="contract_received"')
        copy_review_to_text(user=self.superuser, review_id=review.pk)
        review.refresh_from_db()
        self.assertTrue(review.copied_text.authors.filter(pk=self.author.pk).exists())

    def test_contract_lookup_does_not_accept_different_identity(self):
        self.author.has_contract = True
        self.author.save()
        review = self.review(author_first_name='Inna', status='accepted', author_notified_at=timezone.now())
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('core:assigned_review_detail', args=[review.pk]))
        self.assertFalse(response.context['author_has_contract'])
        with self.assertRaises(ValidationError):
            copy_review_to_text(user=self.superuser, review_id=review.pk)

    def test_notification_date_tracks_boolean_transition_only(self):
        row = Recruitment.objects.create(first_name='Anna', last_name='Żółć', email='a@example.com', notified=True)
        initial = row.notified_at
        self.assertIsNotNone(initial)
        row.notes = 'Zmiana uwag'
        row.save()
        self.assertEqual(row.notified_at, initial)
        row.notified = False
        row.save(update_fields=['notes'])
        row.refresh_from_db()
        self.assertTrue(row.notified)
        self.assertEqual(row.notified_at, initial)
        row.notified = False
        row.save(update_fields=['notified'])
        self.assertIsNone(row.notified_at)
        row.notified = True
        row.save(update_fields=['notified'])
        self.assertGreaterEqual(row.notified_at, initial)
        self.assertEqual(Recruitment.objects.get(pk=row.pk).email, row.email)
        with self.assertRaises(LookupError):
            apps.get_model("people", "Rekrutacja")
        registered = [model for model in admin.site._registry if model._meta.concrete_model is Recruitment]
        self.assertEqual(registered, [Recruitment])

    def test_recruitment_form_statuses_departments_and_timestamp_protection(self):
        data = dict(first_name='Anna', last_name='Test', email='A@example.com', department='audio_proofreaders', submitted_at='2026-01-01', status='accepted', notified='on', notified_at='2000-01-01')
        form = RecruitmentForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        row = form.save()
        self.assertEqual(row.notified_at.date(), timezone.now().date())
        self.assertEqual(len(Recruitment.Department.choices), 12)
        self.assertFalse(RecruitmentForm({**data, 'status':'trial'}).is_valid())
        self.assertFalse(RecruitmentForm({**data, 'department':'invalid'}).is_valid())

    def test_recruitment_has_only_requested_business_fields(self):
        self.assertEqual({field.name for field in Recruitment._meta.fields}, {
            'id', 'first_name', 'last_name', 'email', 'department', 'submitted_at',
            'status', 'notified', 'notified_at', 'notes', 'unofficial_notes', 'updated_at',
        })
        self.assertNotIn('people_rekrutacja', connection.introspection.table_names())

    def test_completed_filter_counts_opinions_not_assignments(self):
        review = self.review(status='in_review')
        users = [create_member(f'additional_{n}', 'Recenzent')[0] for n in range(6)]
        for n,user in enumerate(users,1):
            ReviewAssignment.objects.create(review=review,user=user,position=n,opinion='reading')
        self.assertEqual(len(list(review_list_context(user=self.reviewer,params=QueryDict('completed=0'))['reviews'])),1)
        self.assertEqual(len(list(review_list_context(user=self.reviewer,params=QueryDict('completed=6'))['reviews'])),0)
        ReviewAssignment.objects.filter(review=review).update(opinion='yes')
        self.assertEqual(len(list(review_list_context(user=self.reviewer,params=QueryDict('completed=6'))['reviews'])),1)

    def test_my_reviews_are_private_and_role_restricted(self):
        own = self.review(title='Moja recenzja')
        other = self.review(title='Cudza recenzja')
        ReviewAssignment.objects.create(review=own,user=self.reviewer,position=1,opinion='reading')
        ReviewAssignment.objects.create(review=other,user=self.editor,position=1,opinion='reading')
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('core:my_reviews'))
        self.assertContains(response,'Moja recenzja')
        self.assertNotContains(response,'Cudza recenzja')
        home = self.client.get(reverse('core:home'))
        self.assertTrue(home.context['reviewer_only'])
        self.assertContains(home,'Moja recenzja')
        self.assertNotContains(home,self.author.email)
        for user in (self.editor,self.coordinator,self.superuser):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('core:my_reviews')).status_code,403)

    def test_people_roles_use_or_and_return_no_duplicates(self):
        extra = Role.objects.create(name='Korektor')
        self.editor_person.roles.add(extra)
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('core:people_list'), {'roles':[extra.pk, self.reviewer_person.roles.first().pk]})
        self.assertEqual({person.pk for person in response.context['people']}, {self.editor_person.pk,self.reviewer_person.pk})

    def test_invalid_import_does_not_provide_confirmation_token(self):
        self.client.force_login(self.superuser)
        response = self.client.post(reverse('core:review_bulk_import'), {'import_action':'preview','anthology':self.anthology.pk,'records':'Niepoprawny rekord'})
        self.assertEqual(response.status_code,400)
        self.assertFalse(response.context['can_import'])
        self.assertEqual(response.context['preview_token'],'')
        self.assertFalse(Review.objects.exists())

    def test_all_application_models_registered_in_admin(self):
        for label in ('authors','people','texts','workflow','illustrations','core'):
            for model in apps.get_app_config(label).get_models():
                self.assertTrue(admin.site.is_registered(model),model.__name__)

    def test_long_corrections_expand_without_losing_copy_text(self):
        item = AnthologyCorrection.objects.create(anthology=self.anthology,story_title='Próba',fragment='A'*180,problem='Krótka uwaga',suggestion='B'*180,submitted_by=self.reviewer)
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse('core:anthology_corrections'))
        self.assertContains(response,'<details class="long-cell">',count=2)
        self.assertContains(response,f'data-copy-value="{item.fragment}"')
        self.assertContains(response,'aria-current="page"')

    def test_team_roles_order_and_typesetter_migration(self):
        from people.role_ordering import TEAM_ROLE_ORDER
        from core.forms import PeopleFilterForm
        self.assertTrue(Role.objects.filter(name="Składacz").exists())
        for name in reversed(TEAM_ROLE_ORDER):
            Role.objects.get_or_create(name=name)
        names = list(PeopleFilterForm().fields['roles'].queryset.values_list('name', flat=True))
        self.assertEqual(names[:len(TEAM_ROLE_ORDER)], list(TEAM_ROLE_ORDER))
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('core:people_list'))
        self.assertContains(response, 'data-checkbox-dropdown')
        self.assertContains(response, 'Składacz')
        html = response.content.decode()
        self.assertLess(html.index('Panel administracyjny'), html.index('Moja praca'))
        self.assertGreater(html.index('Wyloguj się'), html.index('Wyszukaj\n'))
        self.assertEqual(apps.get_app_config('authors').verbose_name, 'Autorzy')
        self.assertEqual(apps.get_app_config('people').verbose_name, 'Ludzie')

    def test_superuser_claims_styling_from_detail_without_team_profile(self):
        from core.forms import CoordinatorTextBulkActionForm
        text = Text.objects.create(title="Wolne stylowanie", length=1000)
        stage = WorkflowStage.objects.create(text=text, stage_type="styling")
        self.assertIn(self.superuser, CoordinatorTextBulkActionForm().fields["assigned_to"].queryset)
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("core:assigned_text_detail", args=[text.pk]))
        self.assertContains(response, "Przejmij stylowanie")
        self.assertNotContains(response, "Przejdź do czynności etapu")
        response = self.client.post(reverse("core:take_workflow_stage", args=[stage.pk]), {"started_at": timezone.localdate().isoformat()})
        self.assertEqual(response.status_code, 302)
        stage.refresh_from_db()
        self.assertEqual(stage.started_at, timezone.localdate())
        self.assertEqual(text.workflow_role_assignments.get(role="styling").assigned_to_id, self.superuser.pk)
        response = self.client.get(reverse("core:assigned_text_detail", args=[text.pk]))
        self.assertNotContains(response, "Przejmij stylowanie")
        self.assertContains(response, "Zakończ etap")

    def test_coordinator_cannot_claim_styling_from_detail(self):
        text = Text.objects.create(title="Wolne stylowanie", length=1000)
        stage = WorkflowStage.objects.create(text=text, stage_type="styling")
        self.client.force_login(self.coordinator)
        self.assertNotContains(self.client.get(reverse("core:assigned_text_detail", args=[text.pk])), "Przejmij stylowanie")
        response = self.client.post(reverse("core:take_workflow_stage", args=[stage.pk]), {"started_at": timezone.localdate().isoformat()})
        self.assertEqual(response.status_code, 403)
        stage.refresh_from_db()
        self.assertIsNone(stage.started_at)
