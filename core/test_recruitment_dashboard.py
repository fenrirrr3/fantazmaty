from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.models import Recruitment
from people.models import Person, Role
from texts.models import Anthology, Review


class RecruitmentDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.admin = users.objects.create_superuser('queue-admin', 'admin@example.test', 'test')
        cls.coordinator = users.objects.create_user('queue-coord')
        cls.member = users.objects.create_user('queue-member')
        for user in (cls.coordinator, cls.member):
            person = Person.objects.create(user=user, first_name='Jan', last_name=user.username)
            if user == cls.coordinator:
                person.roles.add(Role.objects.get_or_create(name='Koordynator')[0])
        cls.multi = Recruitment.objects.create(mail_subject='Kilka ról', mail_roles=['editors', 'reviewers', 'editors'])
        cls.manual = Recruitment.objects.create(first_name='Ręcznie', department='editors')
        cls.legacy = Recruitment.objects.create(first_name='Dźwięk', department='sound')
        cls.unknown = Recruitment.objects.create(mail_subject='Bez roli')
        Recruitment.objects.create(mail_roles=['editors'], status='accepted')
        Recruitment.objects.create(mail_roles=['editors'], status='rejected')
        anthology = Anthology.objects.create(title='Antologia kolejki')
        cls.review = Review.objects.create(anthology=anthology, title='Jawny tytuł', length=1000,
            author_first_name='Tajny', author_last_name='Autor', email='secret@example.test',
            status='accepted', author_notified_at=timezone.now())
        Review.objects.create(anthology=anthology, title='Ukryty tytuł', length=1000,
            status='accepted', author_notified_at=timezone.now(), is_hidden=True)

    def test_counts_and_links_match_pending_records_for_both_privileged_roles(self):
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            response = self.client.get(reverse('core:home'))
            counts = {row['role']: row['count'] for row in response.context['pending_recruitment']}
            self.assertEqual(counts, {'editors': 2, 'reviewers': 1, 'sound': 1, 'other': 1})
            doc = html.fromstring(response.content)
            for link in doc.xpath('//*[@data-pending-recruitment]//a'):
                page = self.client.get(link.get('href'))
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.context['page_obj'].paginator.count, int(link.text_content().rsplit(':', 1)[1]))
                self.assertTrue(all(item.status == 'new' for item in page.context['items']))
            self.assertContains(response, 'Jawny tytuł')
            if user == self.coordinator:
                self.assertNotContains(response, 'Tajny Autor')
                self.assertNotContains(response, 'Ukryty tytuł')

    def test_no_pending_hides_entire_card_and_member_cannot_access_queue(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('core:home'))
        self.assertNotContains(response, 'data-pending-recruitment')
        self.assertEqual(self.client.get(reverse('core:recruitment_list')).status_code, 403)
        Recruitment.objects.filter(status='new').update(status='accepted')
        self.client.force_login(self.coordinator)
        self.assertNotContains(self.client.get(reverse('core:home')), 'data-pending-recruitment')

    def test_filters_preserved_with_search_and_pagination(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:recruitment_list'),
            {'role': 'editors', 'status': 'new', 'q': 'Kilka', 'page_size': 10})
        self.assertEqual([item.pk for item in response.context['items']], [self.multi.pk])
        self.assertEqual(response.context['role'], 'editors')
        self.assertEqual(response.context['status'], 'new')
