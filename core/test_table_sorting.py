import json
import re
from html import unescape
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.contrib.auth import get_user_model
from core.pagination import paginate_items
from core.table_sorting import prepare_table_sort
from texts.models import Review, Text, ReviewAssignment
from people.models import Person, Role
from authors.models import Author
from core.models import Recruitment
from workflow.models import WorkflowStage


class CoreTestDataMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        cls.superuser = User.objects.create_superuser(
            username="superadmin", email="superadmin@example.com", password="test-password")
        for attr, role_name in (("reviewer", "Recenzent"), ("editor", "Redaktor")):
            user = User.objects.create_user(username=attr, email=f"{attr}@example.com")
            person = Person.objects.create(first_name=attr, last_name="Testowy", email=user.email, user=user)
            role, _ = Role.objects.get_or_create(name=role_name)
            person.roles.add(role)
            setattr(cls, attr, user)
            setattr(cls, attr + "_person", person)
        cls.author = Author.objects.create(first_name="Autor", last_name="Testowy", email="author@example.com")
        from texts.models import Anthology
        cls.anthology = Anthology.objects.create(title="Antologia testowa")


class TableSortingTests(CoreTestDataMixin, TestCase):
    def request(self, sort, user=None):
        request = RequestFactory().get('/', {'sort': sort})
        request.user = user or self.superuser
        return request

    def test_every_paginated_data_header_has_a_server_sort(self):
        text = Text.objects.create(title='Sortowanie', anthology=self.anthology, length=10)
        text.authors.add(self.author)
        self.client.force_login(self.superuser)
        routes = ['text_list','my_texts','available_texts','workflow_list','review_list','my_reviews',
                  'author_list','people_list','anthology_list','recruitment_list','extract_list',
                  'anthology_corrections','user_activity','notification_queue','scheduled_rejections',
                  'active_vacations']
        for name in routes:
            self.client.force_login(self.reviewer if name == 'my_reviews' else self.superuser)
            with self.subTest(route=name):
                response = self.client.get(reverse('core:' + name))
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                match = re.search(r'id="table-sort-columns"[^>]*>(.*?)</script>', html, re.S)
                if not match:
                    continue
                columns = json.loads(match[1]) or {}
                for head in re.findall(r'<th\b[^>]*>(.*?)</th>', html, re.S):
                    label = unescape(re.sub('<[^>]+>', '', head)).strip()
                    if label and label not in {'Akcje','Akcja','Szczegóły','Powiadomienie'}:
                        self.assertIn(label, columns)
                # Exercise every declared sort, also those hidden behind conditionals.
                for key in set(columns.values()):
                    with self.subTest(sort=key):
                        self.assertEqual(self.client.get(reverse('core:' + name), {'sort':key}).status_code, 200)

    def test_sort_covers_all_pages_and_preserves_values_projection(self):
        Recruitment.objects.bulk_create([Recruitment(first_name=str(i), last_name='Osoba', email=f'{i}@example.com', notes=f'{39-i:02}') for i in range(40)])
        page = paginate_items(self.request('notes'), Recruitment.objects.all())
        self.assertEqual(page.paginator.count, 40)
        self.assertEqual(page.object_list[0].notes, '00')
        page = paginate_items(self.request('-notes'), Recruitment.objects.all())
        self.assertEqual(page.object_list[0].notes, '39')

    def test_roles_do_not_duplicate_people(self):
        role, _ = Role.objects.get_or_create(name='Grafik')
        self.editor_person.roles.add(role)
        items, _ = prepare_table_sort(self.request('roles'), Person.objects.prefetch_related('roles'))
        self.assertEqual(len(items), Person.objects.count())
        self.assertEqual(len({p.pk for p in items}), len(items))

    def test_author_anthologies_support_dict_queryset(self):
        text = Text.objects.create(title='T', anthology=self.anthology, length=1)
        text.authors.add(self.author)
        empty = Author.objects.create(first_name='Bez',last_name='Tekstów',email='empty@example.com')
        for key in ('anthologies','-anthologies'):
            items, _ = prepare_table_sort(self.request(key), Author.objects.values('pk','first_name'))
            self.assertEqual(items[0]['pk'], self.author.pk)
            self.assertEqual(items[-1]['pk'], empty.pk)

    def test_private_sort_is_not_advertised_and_raw_orm_is_ignored(self):
        review = Review.objects.create(title='T', anthology=self.anthology, length=1)
        for key in ('author','email','author__notes__content'):
            items, columns = prepare_table_sort(self.request(key, self.editor), Review.objects.all())
            self.assertNotIn('Autor',columns)
            self.assertEqual(list(items.values_list('pk',flat=True)),[review.pk])

    def test_missing_values_last_in_both_directions(self):
        rows=[{'title':'Łódź','started_at':None},{'title':'Las','started_at':None},{'title':'Żar','started_at':None}]
        items,_=prepare_table_sort(self.request('title'), rows)
        self.assertEqual([r['title'] for r in items],['Las','Łódź','Żar'])

    def test_illustrations_and_workflow_computed_columns_with_data(self):
        from illustrations.models import CoverProposal
        from core.selectors.texts import workflow_list_context, my_texts_context, available_stages_for_user
        from django.http import QueryDict
        from workflow.services import claim_stage
        self.anthology.has_illustrations=True
        self.anthology.save()
        text=Text.objects.create(title='Ilustracja', anthology=self.anthology, length=10)
        text.authors.add(self.author)
        from workflow.models import WorkflowRoleAssignment
        from django.utils import timezone
        assignment=WorkflowRoleAssignment.objects.create(text=text,role='editor',assigned_to=self.editor)
        WorkflowStage.objects.create(text=text,stage_type='editing',assignment=assignment,started_at=timezone.localdate())
        self.client.force_login(self.superuser)
        for route in ('illustrations:illustration_list','illustrations:cover_proposal_list'):
            response=self.client.get(reverse(route))
            self.assertEqual(response.status_code,200)
            columns=response.context['page_obj'].sort_columns
            for key in set(columns.values()):
                with self.subTest(route=route, key=key):
                    self.assertEqual(self.client.get(reverse(route),{'sort':key}).status_code,200)
        for context,key in ((workflow_list_context(user=self.superuser,params=QueryDict()),'stages'),
                            (my_texts_context(user=self.editor,params=QueryDict()),'texts')):
            rows=context[key]
            _,columns=prepare_table_sort(self.request(''),rows)
            for sort in set(columns.values()):
                with self.subTest(sort=sort):
                    result,_=prepare_table_sort(self.request(sort),rows)
                    self.assertEqual(len(list(result)),len(list(rows)))

    def test_review_counts_sort_only_submitted_opinions(self):
        first=Review.objects.create(title='A',anthology=self.anthology,length=1)
        second=Review.objects.create(title='B',anthology=self.anthology,length=1)
        ReviewAssignment.objects.create(review=first,user=self.reviewer,position=1,opinion='reading')
        ReviewAssignment.objects.create(review=second,user=self.reviewer,position=1,opinion='yes')
        rows,_=prepare_table_sort(self.request('-opinions'),Review.objects.all())
        self.assertEqual(list(rows.values_list('pk',flat=True)),[second.pk,first.pk])
