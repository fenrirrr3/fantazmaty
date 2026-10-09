import json
from io import StringIO
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from lxml import html
from authors.models import Author, AuthorNote
from people.models import Person, Role
from texts.models import Anthology, Text, Review, ReviewAssignment, Extract, NovelProfile
from workflow.availability import claim_access, eligible_role_users, role_access_reason
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.read_queries import available_stages
from workflow.services import claim_stage


class LayoutAndRoleTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('layout-admin', 'admin@example.test', 'test')
        self.member = get_user_model().objects.create_user('layout-member')
        self.person = Person.objects.create(user=self.member, first_name='Jan', last_name='Test')
        self.book = Anthology.objects.create(title='Antologia')
        self.text = Text.objects.create(title='Tekst', anthology=self.book, length=100)

    def roles(self, *names):
        self.person.roles.set([Role.objects.get_or_create(name=name)[0] for name in names])

    def test_post_layout_permissions_and_exact_workflow_role(self):
        self.client.force_login(self.member)
        route = reverse('core:post_layout')
        for roles, allowed in [((), False), (('Korektor',), False), (('Koordynator redakcji',), False), (('Koordynator',), False), (('Korektor poskładowy',), True), (('Koordynator korekty',), True)]:
            self.roles(*roles)
            self.assertEqual(self.client.get(route).status_code, 200 if allowed else 403)
            doc = html.fromstring(self.client.get(reverse('core:home')).content)
            self.assertEqual(bool(doc.xpath(f'//nav//a[@href="{route}"]')), allowed)
        self.roles('Korektor poskładowy')
        stage = S.objects.create(text=self.text, stage_type='first_proofreading')
        self.assertTrue(role_access_reason(self.member, A.Role.PROOFREADER_1))
        self.assertFalse(eligible_role_users(A.Role.PROOFREADER_1).filter(pk=self.member.pk).exists())
        self.assertFalse(available_stages(self.member, claim_access(self.member)).exists())
        self.assertNotContains(self.client.get(reverse('core:available_texts')), 'Przejmij</button>', html=False)
        with self.assertRaises(PermissionDenied):
            claim_stage(self.text, stage.stage_type, self.member)
        self.roles('Korektor poskładowy', 'Korektor')
        self.assertFalse(role_access_reason(self.member, A.Role.PROOFREADER_1))
        self.assertEqual(list(available_stages(self.member, claim_access(self.member))), [stage])
        self.person.is_active = False; self.person.save()
        self.assertEqual(self.client.get(route).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(route).status_code, 200)
        self.assertEqual(self.client.get(reverse('core:audio_proofreading')).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(route).status_code, 302)

    def test_text_layout_sections_forms_and_no_stage_pagination(self):
        self.client.force_login(self.admin)
        S.objects.create(text=self.text, stage_type='ready_for_editing')
        for note in ('', 'Notatka testowa'):
            self.text.coordinator_note = note; self.text.save()
            response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
            self.assertEqual(response.status_code, 200)
            doc = html.fromstring(response.content)
            self.assertEqual(len(doc.xpath('//*[@id="text-withdraw"]/summary')), 1)
            self.assertFalse(doc.xpath('//*[@id="text-withdraw"]//h3'))
            self.assertEqual(doc.xpath('//section[@aria-labelledby="workflow-heading"]//table/@data-pagination'), ['off'])
            content = response.content.decode()
            self.assertLess(content.index('Recenzje tekstu'), content.index('id="text-audiobook"'))
            self.assertLess(content.index('id="text-audiobook"'), content.index('id="text-handoff"'))
            self.assertFalse(doc.xpath('//*[contains(@class,"notes-editor-grid")]/p'))
            self.assertTrue(doc.xpath('//textarea[@name="coordinator_note"]'))

    def test_pending_reviews_include_multi_role_user_and_paginate_without_other_users(self):
        self.roles('Recenzent', 'Redaktor')
        self.client.force_login(self.member)
        for n in range(8):
            review = Review.objects.create(title=f'Oczekuje {n}', anthology=self.book, length=100)
            ReviewAssignment.objects.create(review=review, user=self.member, position=1, opinion='reading')
        for title, kwargs, opinion in [('Oddana', {}, 'yes'), ('Archiwum', {'old_reviews': True}, 'reading'), ('Ukryta', {'is_hidden': True}, 'reading'), ('Zamknięta', {'status': 'accepted'}, 'reading')]:
            review = Review.objects.create(title=title, anthology=self.book, length=100, **kwargs)
            ReviewAssignment.objects.create(review=review, user=self.member, position=1, opinion=opinion)
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.context['pending_review_count'], 8)
        self.assertEqual(len(response.context['pending_reviews']), 6)
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//*[contains(@class,"dashboard-right-stack")]/section')), 2)
        second = self.client.get(reverse('core:dashboard_tasks'), {'kind': 'reviews', 'page': 2}).json()
        self.assertEqual(second['html'].count('dashboard-list-item'), 2)
        self.assertIsNone(second['next_url'])
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('core:home')).context['pending_review_count'], 0)

    def test_reviews_filters_no_bulk_and_dashboard_pair(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:review_list'))
        self.assertNotContains(response, 'Operacje zbiorcze')
        self.assertNotContains(response, 'review-bulk-form')
        doc = html.fromstring(response.content)
        self.assertTrue(doc.xpath('//*[contains(@class,"review-filter-bottom")]//*[@data-active-filters-target]'))
        response = self.client.get(reverse('core:author_list'))
        self.assertNotContains(response, 'brak wyboru')
        doc = html.fromstring(self.client.get(reverse('core:home')).content)
        headings = doc.xpath('//*[contains(@class,"dashboard-intake-grid")]/section/h2/text()')
        self.assertEqual(len(headings), 2)


class AuthorMergeTests(TestCase):
    def setUp(self):
        self.target = Author.objects.create(first_name='Agata', last_name='Bisiecka', email='agata@example.test')
        self.source = Author.objects.create(first_name='Wiktor', last_name='Orłowski', email='wiktor@example.test', has_contract=True, contact=False)
        self.book = Anthology.objects.create(title='Antologia')
        self.text = Text.objects.create(title='Opowiadanie', anthology=self.book, length=100)
        self.text.authors.add(self.target, self.source)
        self.review = Review.objects.create(title='Zgłoszenie', anthology=self.book, author=self.source, length=100, status='accepted')
        self.review.coauthors.add(self.target, self.source)
        AuthorNote.objects.create(author=self.source, content='Stara notatka')

    def run_merge(self, **kwargs):
        output = StringIO()
        call_command('merge_bisiecka_author', stdout=output, **kwargs)
        return json.loads(output.getvalue())

    def test_dry_run_apply_relations_history_and_idempotence(self):
        person = Person.objects.create(first_name='Agata', last_name='Bisiecka', author_profile=self.source)
        novel = Anthology.objects.create(title='Powieść', is_novel=True).novel
        novel.authors.add(self.source)
        original_date = self.review.created_at
        self.run_merge()
        self.assertTrue(Author.objects.filter(pk=self.source.pk).exists())
        self.target.refresh_from_db(); self.assertEqual(self.target.pseudonym, '')
        result = self.run_merge(apply=True)
        self.target.refresh_from_db(); person.refresh_from_db(); self.review.refresh_from_db()
        self.assertEqual(self.target.pseudonym, 'Wiktor Orłowski')
        self.assertEqual(self.target.email, 'agata@example.test')
        self.assertFalse(self.target.contact)
        self.assertTrue(self.target.has_contract)
        self.assertEqual(person.author_profile_id, self.target.pk)
        self.assertEqual(self.review.author_id, self.target.pk)
        self.assertEqual(self.review.created_at, original_date)
        self.assertEqual(self.review.status, 'accepted')
        self.assertFalse(self.review.coauthors.exists())
        self.assertEqual(list(self.text.authors.all()), [self.target])
        self.assertEqual(list(novel.authors.all()), [self.target])
        self.assertFalse(Author.objects.filter(pk=self.source.pk).exists())
        self.assertTrue(self.target.notes.filter(content='Stara notatka').exists())
        self.assertTrue(self.target.notes.filter(content__contains='wiktor@example.test').exists())
        self.assertTrue(result['warnings'])
        count = self.target.notes.count()
        self.run_merge(apply=True)
        self.assertEqual(self.target.notes.count(), count)

    def test_extracts_combine_without_loss_and_conflicting_decisions_rollback(self):
        first = Extract.objects.create(author=self.target, full_name='Agata', email=self.target.email, recruitment='Nabór', title='A', accepted_titles='A')
        second = Extract.objects.create(author=self.source, full_name='Wiktor', email=self.source.email, recruitment='Nabór', title='A', rejected_titles='A')
        with self.assertRaises(CommandError):
            self.run_merge(apply=True)
        self.target.refresh_from_db(); self.assertEqual(self.target.pseudonym, '')
        self.assertEqual(Extract.objects.count(), 2)
        second.title = 'B'; second.rejected_titles = 'B'; second.save()
        self.run_merge(apply=True)
        first.refresh_from_db()
        self.assertEqual(Extract.objects.count(), 1)
        self.assertEqual(first.title, 'A\nB')
        self.assertEqual(first.status, 'mixed')

    def test_ambiguous_identity_and_two_person_profiles_abort(self):
        Person.objects.create(first_name='A', last_name='B', author_profile=self.source)
        Person.objects.create(first_name='C', last_name='D', author_profile=self.target)
        with self.assertRaises(CommandError):
            self.run_merge(apply=True)
        self.assertTrue(Author.objects.filter(pk=self.source.pk).exists())
        Author.objects.create(first_name='Agata', last_name='Bisiecka', email=None)
        with self.assertRaises(CommandError):
            self.run_merge(apply=True)

    def test_empty_email_is_transferred_and_foreign_legacy_link_retained(self):
        from texts.models import ForeignAuthor
        self.target.email = None; self.target.save()
        foreign = ForeignAuthor.objects.create(first_name='Wiktor', last_name='Orłowski', legacy_author_id=self.source.pk)
        self.run_merge(apply=True)
        self.target.refresh_from_db(); foreign.refresh_from_db()
        self.assertEqual(self.target.email, 'wiktor@example.test')
        self.assertEqual(foreign.legacy_author_id, self.target.pk)
