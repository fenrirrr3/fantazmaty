"""Zgody na newsletter: łączenie zgód z wielu zgłoszeń i lista dostępna tylko dla superużytkownika."""
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import NewsletterConsent
from core.pagination import paginate_items
from core.services.newsletters import record_consents
from fantazmaty.testing import create_member, create_superuser


class NewsletterListTests(TestCase):
    def test_consents_merge_and_filter_superuser_only(self):
        record_consents('JAN@example.com', premieres=True)
        record_consents('jan@example.com', recruitment=True)
        record_consents('jan@example.com')
        record_consents('other@example.com')
        self.assertEqual(NewsletterConsent.objects.count(), 2)
        url = reverse('core:newsletter_list')
        self.client.force_login(create_member('coord', 'Koordynator recenzji'))
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(create_superuser())
        response = self.client.get(url, {'consent': 'general'})
        self.assertContains(response, 'jan@example.com')
        self.assertNotContains(response, 'other@example.com')

    def test_newsletter_sort_is_global_and_has_all_columns(self):
        """Sortowanie obejmuje wszystkie strony listy, a nie tylko bieżącą."""
        NewsletterConsent.objects.bulk_create(
            [NewsletterConsent(email=f'{i:02}@example.test', premieres=i % 2 == 0) for i in range(35)])
        request = RequestFactory().get('/', {'sort': '-email'})
        request.user = create_superuser()
        page = paginate_items(request, NewsletterConsent.objects.all())
        self.assertEqual(page.object_list[0].email, '34@example.test')
        self.assertIn('Zgoda na newsletter o premierach', page.sort_columns)

    def test_consent_without_email_is_ignored(self):
        for email in ('', '   ', None):
            with self.subTest(email=email):
                record_consents(email, premieres=True, recruitment=True)
        self.assertFalse(NewsletterConsent.objects.exists())

