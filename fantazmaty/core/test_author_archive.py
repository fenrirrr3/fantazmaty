from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from authors.models import Author
from texts.models import Anthology, Text, Review
from workflow.models import WorkflowStage

class AuthorArchiveTests(TestCase):
    def setUp(self):
        self.admin=get_user_model().objects.create_superuser(username='admin',password='test')
        self.client.force_login(self.admin)
        self.anthology=Anthology.objects.create(title='Tom A')
        self.other=Anthology.objects.create(title='Tom B')
        self.ready=Author.objects.create(first_name='Anna',last_name='Gotowa')
        self.pending=Author.objects.create(first_name='Jan',last_name='Roboczy')
        self.empty=Author.objects.create(first_name='Ewa',last_name='Beztekstów')
        self.text(self.ready,self.anthology,'ready')
        self.text(self.pending,self.anthology,'ready_for_editing')

    def text(self, author, anthology, stage, **kwargs):
        text=Text.objects.create(title='Tekst '+author.last_name,anthology=anthology,length=100,**kwargs)
        text.authors.add(author)
        WorkflowStage.objects.create(text=text,stage_type=stage,workflow_cycle=text.current_workflow_cycle)
        return text

    def author_ids(self, params=None):
        response=self.client.get(reverse('core:author_list'),params or {})
        self.assertEqual(response.status_code,200)
        return {a['pk'] for a in response.context['authors']}

    def test_default_and_off(self):
        self.assertEqual(self.author_ids(),{self.ready.pk})
        self.assertEqual(self.author_ids({'accepted':'0'}),{self.ready.pk,self.pending.pk,self.empty.pk})
        self.assertEqual(self.author_ids({'accepted':'1','q':'Beztekstów'}),set())
        self.assertEqual(self.author_ids({'accepted':'0','q':'Beztekstów'}),{self.empty.pk})

    def test_ready_must_be_in_selected_anthology(self):
        self.text(self.ready,self.other,'ready_for_editing')
        self.assertEqual(self.author_ids({'anthology':self.other.pk}),set())
        self.assertEqual(self.author_ids({'anthology':self.other.pk,'accepted':'0'}),{self.ready.pk})

    def test_historical_ready_and_withdrawn_priority(self):
        text=self.text(self.empty,self.anthology,'ready',is_historical=True)
        self.assertIn(self.empty.pk,self.author_ids())
        WorkflowStage.objects.create(text=text,stage_type='withdrawn',workflow_cycle=1)
        self.assertNotIn(self.empty.pk,self.author_ids())

    def test_archive_includes_coauthor_but_not_current_or_other_author(self):
        def review(title,old,author):
            return Review.objects.create(title=title,old_reviews=old,author=author,
                author_first_name=author.first_name,author_last_name=author.last_name,
                anthology=self.anthology,length=100,email='a@example.com',genre='fantasy')
        historical=review('Dawne zgłoszenie',True,self.ready)
        coauthored=review('Wspólne zgłoszenie',True,self.pending)
        coauthored.coauthors.add(self.ready)
        review('Bieżące zgłoszenie',False,self.ready)
        review('Cudze zgłoszenie',True,self.empty)
        response=self.client.get(reverse('core:author_detail',args=[self.ready.pk]))
        self.assertEqual(response.status_code,200)
        self.assertEqual({r.pk for r in response.context['historical_reviews']},{historical.pk,coauthored.pk})
        self.assertContains(response,'Archiwalne recenzje')
        self.assertContains(response,reverse('core:assigned_review_detail',args=[coauthored.pk]))
        self.assertEqual(response.context['author_summary']['submissions'],1)

    def test_non_superuser_cannot_access_archive(self):
        user=get_user_model().objects.create_user(username='ordinary',password='test')
        self.client.force_login(user)
        response=self.client.get(reverse('core:author_detail',args=[self.ready.pk]))
        self.assertEqual(response.status_code,403)
        self.assertNotContains(response,'Archiwalne recenzje',status_code=403)
