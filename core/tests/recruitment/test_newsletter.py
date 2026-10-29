"""Zgody na newsletter: wartości z formularzy (nowe „tak”/„tylko nabory” i dawne „premierach”/„naborach”),
łączenie zgód z wielu zgłoszeń i lista dostępna tylko dla superużytkownika."""
from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from core.models import NewsletterConsent
from core.pagination import paginate_items
from core.services.mailbox_import import parse_message
from core.services.newsletters import parse_consents, record_consents
from core.services.review_import_parser import parse_review_records
from core.tests.recruitment.support import NAMED_BODY, named_mail
from fantazmaty.testing import create_member, create_superuser


class ConsentValuesTests(SimpleTestCase):
    def test_new_and_former_form_values(self):
        """„tak” – newsletter ogólny (dawniej „premierach”), „tylko nabory” – nabory (dawniej „naborach”), „nie”
        i puste pole – brak zgody; wielkość liter, spacje i przecinki bez znaczenia."""
        for value, expected in {
            'tak': (True, False), 'Tak': (True, False), 'tylko nabory': (False, True), 'Tylko  Nabory': (False, True),
            'tak, tylko nabory': (True, True), 'tylko nabory,tak': (True, True), '': (False, False), '  ': (False, False),
            'nie': (False, False), 'premierach': (True, False), 'naborach': (False, True),
            'premierach, naborach': (True, True), 'premierach naborach': (True, True), 'tak, naborach': (True, True),
        }.items():
            with self.subTest(value=value):
                consents = parse_consents(value)
                self.assertEqual((consents['premieres'], consents['recruitment']), expected)

    def test_unknown_values_are_rejected(self):
        for value in ('tylko', 'nabory', 'tak tylko', 'tylko premiery', 'wszystko', 'tak, literowka'):
            with self.subTest(value=value), self.assertRaisesMessage(ValidationError, '„tak”, „tylko nabory”'):
                parse_consents(value)

    def test_mail_and_manual_import_read_new_values(self):
        """Mail z formularza i ręczny import: „Newsletter: tylko nabory” daje samą zgodę na nabory."""
        for value, expected in {'tak': (True, False), 'tylko nabory': (False, True), '': (False, False)}.items():
            with self.subTest(value=value):
                body = NAMED_BODY.replace('Newsletter: premierach, naborach', f'Newsletter: {value}')
                row = parse_message(1, named_mail(body))
                self.assertEqual((row['newsletter_premieres'], row['newsletter_recruitment']), expected)
                record = parse_review_records(row['record'])[0][0]
                self.assertEqual((record['newsletter_premieres'], record['newsletter_recruitment']), expected)


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
        for text in ('<th>Newsletter ogólny</th><th>Tylko nabory</th>', '>Tylko nabory</option>'):
            self.assertContains(response, text, html=False)
        self.assertNotContains(response, 'other@example.com')

    def test_newsletter_sort_is_global_and_has_all_columns(self):
        """Sortowanie obejmuje wszystkie strony listy, a nie tylko bieżącą."""
        NewsletterConsent.objects.bulk_create(
            [NewsletterConsent(email=f'{i:02}@example.test', premieres=i % 2 == 0) for i in range(35)])
        request = RequestFactory().get('/', {'sort': '-email'})
        request.user = create_superuser()
        page = paginate_items(request, NewsletterConsent.objects.all())
        self.assertEqual(page.object_list[0].email, '34@example.test')
        self.assertEqual(list(page.sort_columns)[1:3], ['Newsletter ogólny', 'Tylko nabory'])

    def test_consent_without_email_is_ignored(self):
        for email in ('', '   ', None):
            with self.subTest(email=email):
                record_consents(email, premieres=True, recruitment=True)
        self.assertFalse(NewsletterConsent.objects.exists())

