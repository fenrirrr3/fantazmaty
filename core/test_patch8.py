from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import Permission
from core.tests import CoreTestDataMixin
from core.models import Recruitment
from texts.models import Extract


class Patch8Tests(CoreTestDataMixin, TestCase):
    def test_admin_access_requires_coordinator_even_with_model_permissions(self):
        for user, expected in ((self.reviewer, 403), (self.coordinator, 200), (self.superuser, 200)):
            user.is_staff = True
            user.save(update_fields=['is_staff'])
            user.user_permissions.add(*Permission.objects.filter(content_type__model__in=['recruitment', 'extract']))
            self.client.force_login(user)
            for model in ('core_recruitment', 'texts_extract'):
                for operation in ('changelist', 'add'):
                    self.assertEqual(self.client.get(reverse(f'admin:{model}_{operation}')).status_code, expected)

    def test_admin_creates_records_and_validates_department(self):
        self.coordinator.is_staff = True
        self.coordinator.save(update_fields=['is_staff'])
        self.client.force_login(self.coordinator)
        data = {'first_name':'Anna', 'last_name':'Test', 'email':'anna@example.com', 'department':'invalid', 'submitted_at':'2026-09-11', 'status':'new', '_save':'Zapisz'}
        self.client.post(reverse('admin:core_recruitment_add'), data)
        self.assertFalse(Recruitment.objects.exists())
        data['department'] = 'editors'
        self.assertEqual(self.client.post(reverse('admin:core_recruitment_add'), data).status_code, 302)
        self.assertEqual(self.client.post(reverse('admin:texts_extract_add'), {'author':self.author.pk, 'title':'Ekstrakt', 'submission_dates':'2026-09-11', 'recruitment':'Jesień', 'status':'new', '_save':'Zapisz'}).status_code, 302)
        self.assertEqual(Extract.objects.get().email, self.author.email)
        index = self.client.get(reverse('admin:index'))
        self.assertNotContains(index, 'Widoki CMS-a')
        self.assertNotContains(index, '<h2>Zespół</h2>')
        self.assertNotContains(index, '<h2>Publikacje</h2>')
        self.assertContains(index, reverse('admin:core_recruitment_changelist'))
        self.assertContains(index, reverse('admin:texts_extract_changelist'))

    def test_sidebar_sections_and_visibility(self):
        self.client.force_login(self.coordinator)
        html = self.client.get(reverse('core:home')).content.decode()
        publications = html.split('>Publikacje</p>')[1].split('>Zespół</p>')[0]
        team = html.split('>Zespół</p>')[1].split('>Aktywności</p>')[0]
        self.assertIn(reverse('core:extract_list'), publications)
        self.assertGreater(publications.index('>Ekstrakty</a>'), publications.index('>Uwagi do antologii</a>'))
        self.assertIn(reverse('core:recruitment_list'), team)
        self.assertNotIn('>Nabory</p>', html)
        self.assertEqual(html.count('>Aktywność redaktorów</a>'), 1)
        self.client.force_login(self.reviewer)
        html = self.client.get(reverse('core:home')).content.decode()
        for route in ('extract_list', 'recruitment_list', 'editor_activity'):
            self.assertNotIn(reverse(f'core:{route}'), html)
