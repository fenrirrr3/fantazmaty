import re
from django.test import TestCase, RequestFactory
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.templatetags.static import static
from authors.models import Author
from texts.models import Text, Anthology, Review
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from people.models import Person
from django.utils import timezone


class AdminNavigationTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_superuser('admin','admin@example.com','test')
        self.client.force_login(self.user)
        self.book=Anthology.objects.create(title='Test')
        self.author=Author.objects.create(first_name='Jan',last_name='Autor',email='autor@example.com')
        self.text=Text.objects.create(title='Tekst',length=100,anthology=self.book)
        self.text.authors.add(self.author)
        self.person=Person.objects.create(user=self.user,first_name='Jan',last_name='Administrator',email=self.user.email,is_active=True)

    def test_task_groups_order_and_details(self):
        request=RequestFactory().get(reverse('admin:index'));request.user=self.user
        groups=admin.site.get_app_list(request)
        self.assertEqual([g['app_label'] for g in groups],['submissions','publication','translations','authors','team','art','history','settings'])
        self.assertEqual([g['collapsed'] for g in groups],[False]*4+[True]*4)
        by_label = {group['app_label']: group for group in groups}
        def names(group):return [m['object_name'].lower() for m in group['models']]
        self.assertIn('anthologytask',names(groups[1]));self.assertNotIn('illustration',names(groups[1]))
        self.assertIn('texttranslation',names(by_label['translations']))
        self.assertIn('workflowstage',names(by_label['history']));self.assertIn('workflowroleassignment',names(by_label['history']))
        detail=by_label['history']['subgroups'][0]
        self.assertEqual(detail['name'],'Szczegółowe rekordy');self.assertTrue(detail['collapsed'])
        self.assertIn('textnote',names(detail));self.assertIn('authornote',names(detail))
        self.assertIn('reviewassignment',names(detail))

    def test_navigation_same_on_index_and_form_and_shortcuts(self):
        for url in (reverse('admin:index'),reverse('admin:texts_text_change',args=[self.text.pk])):
            page=self.client.get(url)
            self.assertEqual(page.status_code,200)
            for label in ('Zgłoszenia i recenzje','Teksty i antologie','Ilustracje i okładki','Historia i diagnostyka','Szczegółowe rekordy'):
                self.assertContains(page,label)
            self.assertContains(page,'data-admin-group="settings"')
            self.assertContains(page,'data-cms-admin-user="'+str(self.user.pk)+'"')
            for route in ('texts_text','texts_review','people_person','texts_anthology'):
                self.assertContains(page,reverse('admin:'+route+'_changelist'))
            self.assertContains(page,static('core/admin-navigation.js'))
        page=self.client.get(reverse('admin:index'))
        group_ids=re.findall(r'<details id="([^"]+)"',page.content.decode())
        self.assertEqual(len(group_ids),len(set(group_ids)))

    def test_text_fieldsets_and_inlines_display_once_in_order(self):
        a=A.objects.create(text=self.text,role='editor',assigned_to=self.user)
        S.objects.create(text=self.text,stage_type='editing',assignment=a,started_at=timezone.localdate())
        content=self.client.get(reverse('admin:texts_text_change',args=[self.text.pk])).content.decode()
        markers=['id="id_title"','Status i zarządzanie','Workflow – wykonawcy etapów','Notatki koordynatora','Notatki do tekstu','Dane techniczne i pochodzenie importu']
        self.assertEqual([marker for marker in markers if marker not in content],[])
        positions=[content.index(marker) for marker in markers]
        self.assertEqual(positions,sorted(positions))
        for marker in ('id="id_title"','id="id_coordinator_note"','id="workflow_stages-group"'):
            self.assertEqual(content.count(marker),1)

    def test_review_fields_then_votes_then_source_link(self):
        review=Review.objects.create(title='Zgłoszenie',anthology=self.book,author=self.author,
            author_first_name='Jan',author_last_name='Autor',email=self.author.email,length=100,genre='fantasy')
        content=self.client.get(reverse('admin:texts_review_change',args=[review.pk])).content.decode()
        markers=['id="id_title"','Autor – wyłącznie superuser','Decyzja','Oceny recenzentów','Ogólne uwagi do zgłoszenia','Proces wydawniczy','Kontrola zgłoszenia']
        self.assertEqual([marker for marker in markers if marker not in content],[])
        positions=[content.index(marker) for marker in markers]
        self.assertEqual(positions,sorted(positions))
        self.assertEqual(content.count('id="id_copied_text"'),1)

    def test_anthology_tasks_before_cover_and_print(self):
        content=self.client.get(reverse('admin:texts_anthology_change',args=[self.book.pk])).content.decode()
        markers=['id="id_title"','id="production_tasks-group"','id="id_cover_status"','id="id_print_status"']
        self.assertEqual([marker for marker in markers if marker not in content],[])
        positions=[content.index(marker) for marker in markers]
        self.assertEqual(positions,sorted(positions))

    def test_non_superuser_cannot_access_panel(self):
        user=get_user_model().objects.create_user('staff',is_staff=True)
        self.client.force_login(user)
        page=self.client.get(reverse('admin:index'))
        self.assertEqual(page.status_code,302)
        self.assertIn(reverse('admin:login'),page.url)
