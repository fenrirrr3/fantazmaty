from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from lxml import html

from core.models import Recruitment


class EmailCopyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('email-copy-admin', 'a@example.test', 'test-only')
        cls.member = get_user_model().objects.create_user('email-copy-member', 'm@example.test', 'test-only')
        from people.models import Person
        Person.objects.create(user=cls.member, first_name='Członek', last_name='Zespołu', email='m@example.test')
        Recruitment.objects.bulk_create([Recruitment(first_name='Pasuje', last_name=str(i), email=f'p{i}@example.test') for i in range(510)])
        Recruitment.objects.create(first_name='Inny', last_name='Kandydat', email='excluded@example.test')

    def test_filtered_copy_pages_include_all_matches_without_changing_preference(self):
        self.client.force_login(self.admin)
        url = reverse('core:recruitment_list')
        self.client.get(url, {'page_size': 50})
        emails = set()
        for page, expected in [(1, 500), (2, 10)]:
            response = self.client.get(url, {'q': 'Pasuje', 'page_size': 500, 'page': page}, HTTP_X_CMS_EMAIL_COPY='1')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.context['items']), expected)
            doc = html.fromstring(response.content)
            self.assertEqual(doc.xpath('string(//nav[@data-sort-config]/@data-result-count)'), '510')
            emails.update(item.email for item in response.context['items'])
        self.assertEqual(len(emails), 510)
        self.assertNotIn('excluded@example.test', emails)
        self.assertEqual(self.client.get(url).context['page_obj'].paginator.per_page, 50)

    def test_clipboard_control_only_for_superuser_and_theme_buttons_exist(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:recruitment_list'))
        self.assertContains(response, 'data-email-copy-toggle')
        self.assertContains(response, 'data-theme-autumn')
        self.assertContains(response, 'data-theme-toggle')
        self.client.force_login(self.member)
        response = self.client.get(reverse('core:home'))
        self.assertNotContains(response, 'data-email-copy-toggle')
        self.assertNotContains(response, 'core/email-copy.js')


class EmailThemeJavaScriptTests(SimpleTestCase):
    @skipUnless(shutil.which('node'), 'Test JavaScript wymaga Node.js.')
    def test_copy_filters_pagination_failure_and_theme_persistence(self):
        script = Path(__file__).with_name('js_tests') / 'email_theme.cjs'
        result = subprocess.run([shutil.which('node'), str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
