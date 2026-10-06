from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from people.models import Person, Role, format_local_datetime


class InterfaceV24Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('admin-ui', 'admin-ui@example.test', 'test')
        cls.member = User.objects.create_user('member-ui', email='member-ui@example.test')
        cls.member.date_joined = timezone.make_aware(datetime(2020, 6, 8, 12, 30))
        cls.member.last_login = timezone.make_aware(datetime(2026, 10, 1, 15, 45))
        cls.member.save()
        cls.person = Person.objects.create(user=cls.member, first_name='Ala', last_name='Osoba',
                                          email=cls.member.email, illustrator_preferences='Zachowaj')
        cls.role, _ = Role.objects.get_or_create(name='Ilustrator')
        cls.artist = Person.objects.create(first_name='Jan', last_name='Ilustrator',
                                          email='artist-ui@example.test', is_active=False)
        cls.artist.roles.add(cls.role)

    def setUp(self):
        activity = patch('core.activity_spool.enqueue_activity')
        activity.start()
        self.addCleanup(activity.stop)
        self.client.force_login(self.admin)

    def person_page(self, person):
        return self.client.get(reverse('admin:people_person_change', args=[person.pk]))

    def test_illustrator_fields_only_for_illustrator_role_even_without_account(self):
        ordinary = self.person_page(self.person)
        self.assertEqual(ordinary.status_code, 200)
        for field in ('illustrator_portfolio', 'illustrator_preferences', 'illustrator_covers', 'illustrator_active'):
            self.assertNotContains(ordinary, f'name="{field}"')
            self.assertContains(self.person_page(self.artist), f'name="{field}"')
        self.person.roles.add(self.role)
        self.assertContains(self.person_page(self.person), 'Ilustrator – portfolio i preferencje')
        self.person.roles.remove(self.role)
        self.assertNotContains(self.person_page(self.person), 'name="illustrator_portfolio"')
        self.person.refresh_from_db()
        self.assertEqual(self.person.illustrator_preferences, 'Zachowaj')

    def test_dates_share_row_with_leave_and_have_empty_states(self):
        page = self.person_page(self.person)
        doc = html.fromstring(page.content.decode())
        halves = doc.xpath('//div[@class="person-admin-fieldsets"]/fieldset[contains(@class,"person-account-half")]')
        self.assertEqual(len(halves), 2)
        self.assertIn('Urlop', halves[0].text_content())
        self.assertIn('Aktywność konta', halves[1].text_content())
        self.assertContains(page, format_local_datetime(self.member.date_joined))
        self.assertContains(page, format_local_datetime(self.member.last_login))
        self.assertNotContains(page, 'name="account_date_joined"')
        self.member.last_login = None
        self.member.save(update_fields=['last_login'])
        self.assertContains(self.person_page(self.person), 'Jeszcze się nie logowano')
        self.assertContains(self.person_page(self.artist), 'Brak powiązanego konta')

    def test_non_illustrator_save_does_not_erase_hidden_illustrator_data(self):
        token = html.fromstring(self.person_page(self.person).content).xpath('//input[@name="_edit_version"]/@value')[0]
        response = self.client.post(reverse('admin:people_person_change', args=[self.person.pk]), {
            'first_name': 'Alicja', 'last_name': 'Osoba', 'email': self.person.email,
            'is_active': 'on', '_save': 'Zapisz', '_edit_version': token,
        })
        self.assertEqual(response.status_code, 302)
        self.person.refresh_from_db()
        self.assertEqual(self.person.first_name, 'Alicja')
        self.assertEqual(self.person.illustrator_preferences, 'Zachowaj')

    def test_header_uses_same_search_route_and_names_without_duplicate_ids(self):
        for user in (self.admin, self.member):
            self.client.force_login(user)
            for route in ('core:home', 'core:global_search'):
                page = self.client.get(reverse(route))
                self.assertEqual(page.status_code, 200)
                doc = html.fromstring(page.content.decode())
                form = doc.xpath('//*[@data-header-search]//form')[0]
                self.assertEqual(form.get('action'), reverse('core:global_search'))
                self.assertEqual(form.get('method'), 'get')
                query = form.xpath('.//input[@name="query"]')[0]
                self.assertEqual(query.get('id'), 'header_query')
                self.assertEqual(query.get('maxlength'), '255')
                self.assertEqual('pseudonim' in query.get('placeholder'), user.is_superuser)
                ids = doc.xpath('//*[@id]/@id')
                self.assertEqual(len(ids), len(set(ids)))

    def test_header_search_hidden_for_anonymous_or_inactive_team_profile(self):
        self.client.logout()
        self.assertNotContains(self.client.get(reverse('login')), 'data-header-search')
        self.person.is_active = False
        self.person.save(update_fields=['is_active'])
        self.client.force_login(self.member)
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.user = self.member
        self.assertNotIn('data-header-search', render_to_string('core/base.html', request=request))
