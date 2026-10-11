"""Wyszukiwarka: wyszukiwanie globalne (sekcje, uprawnienia, prywatność, stronicowanie sekcji),
wyszukiwarka w nagłówku i polskie dopasowanie bez znaków diakrytycznych (SQLite w testach i SQL
dla produkcyjnego MySQL)."""
from datetime import datetime

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.db.backends.mysql.base import DatabaseWrapper as MySQLWrapper
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.models import AudioContributor, Recruitment
from core.views.search import _search_reviews
from fantazmaty.testing import create_member, create_superuser
from illustrations.models import Illustrator
from people.models import Person
from texts.models import Anthology, Review, Text

from .support import NavigationDataMixin


class GlobalSearchTests(NavigationDataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("core:global_search")

    def test_search_hides_empty_sections_and_preserves_result_order_and_markup(self):
        doc = html.fromstring(self.client.get(self.url, {"q": "ZZZnothingmatch"}).content)
        self.assertEqual(doc.xpath('//div[@class="global-search-results"]/section'), [])
        self.assertIn('Brak wyników dla podanego zapytania.', doc.text_content())
        AudioContributor.objects.create(name="Wspólny Lektor")
        Illustrator.objects.create(first_name="Wspólny", last_name="Ilustrator")
        Recruitment.objects.create(first_name="Wspólny", last_name="Kandydat", email="candidate@example.test")
        Anthology.objects.create(title="Wspólny tytuł powieści", is_novel=True)
        doc = html.fromstring(self.client.get(self.url, {"q": "Wspólny"}).content)
        expected = ["Powieści", "Lektorzy i montaż", "Ilustratorzy", "Rekrutacja"]
        self.assertEqual(doc.xpath('//div[@class="global-search-results"]/section/header/h2/text()'), expected)
        for label in expected:
            section = doc.xpath('//section[header/h2[text()="' + label + '"]]')[0]
            self.assertTrue(
                section.xpath('./ul[@class="search-result-list"]/li[@class="search-result-item"]')
            )

    def test_search_includes_external_inactive_and_independent_pages(self):
        """Każda sekcja ma własne stronicowanie po 15 wyników; osoby zewnętrzne i nieaktywne też są."""
        external = create_member("external-needle", "Redaktor").person_profile
        external.first_name = "NeedleExternal"
        external.is_external = True
        external.save()
        inactive = create_member("inactive-needle", "Redaktor").person_profile
        inactive.first_name = "NeedleInactive"
        inactive.is_active = False
        inactive.save()
        book = Anthology.objects.create(title="Testowy tom")
        for i in range(18):
            Text.objects.create(title=f"Needle {i:03}", anthology=book, length=1)
            AudioContributor.objects.create(name=f"NeedleAudio {i:03}")
        page = self.client.get(self.url, {"q": "Needle"})
        self.assertEqual(len(page.context["texts"]), 15)
        self.assertEqual(page.context["texts"].total, 18)
        self.assertEqual({p["pk"] for p in page.context["people"]}, {external.pk, inactive.pk})
        self.assertContains(page, "Zewnętrzny")
        self.assertContains(page, "Nieaktywny")
        self.assertEqual(
            self.client.get(reverse("core:person_detail", args=[inactive.pk])).status_code, 200
        )
        next_url = page.context["texts"].next_url
        self.assertIn("texts_page=2", next_url)
        self.assertIn("q=Needle", next_url)
        next_page = self.client.get(self.url + next_url)
        self.assertEqual(len(next_page.context["texts"]), 3)
        self.assertEqual(len(next_page.context["additional_results"][0]["items"]), 15)
        self.assertEqual(next_page.context["additional_results"][0]["items"].page.number, 1)
        page = self.client.get(self.url, {"q": "Needle", "audio_page": 2})
        self.assertEqual(len(page.context["additional_results"][0]["items"]), 3)
        self.assertEqual(page.context["texts"].page.number, 1)
        self.assertEqual(self.client.get(self.url, {"q": "Needle", "texts_page": "oops"}).status_code, 200)
        self.client.force_login(self.member)
        self.assertEqual(len(self.client.get(self.url, {"q": "Needle"}).context["additional_results"]), 1)

    def test_search_expansion_preserves_permissions_and_public_pseudonym(self):
        for user, allowed in ((self.member, False), (self.manager, True)):
            self.client.force_login(user)
            response = self.client.get(self.url, {'q': 'Nawigator'})
            self.assertContains(response, self.audio.name)
            check = self.assertContains if allowed else self.assertNotContains
            check(response, self.applicant.full_name)
            check(response, str(self.artist))
            self.assertNotContains(response, 'secret-audio@example.test')
            self.assertNotContains(response, 'secret-candidate@example.test')
        self.client.force_login(self.admin)
        response = self.client.get(self.url, {'q': 'Signature'})
        self.assertContains(response, self.text.title)
        self.assertContains(response, reverse('core:translation_detail', args=[self.text.pk]))
        self.assertNotContains(response, 'Secret Name')

    def test_review_search_finds_decided_reviews_hides_withdrawn_and_keeps_author_privacy(self):
        review = Review.objects.create(
            title='Archiwalny test', anthology=Anthology.objects.create(title='Nabór'), author_first_name='Jan', author_last_name='Test',
            email='jan@example.com', author_pseudonym='Pseudonim', length=100, old_reviews=True,
            status='accepted', author_notified_at=timezone.localdate())
        results = _search_reviews('Archiwalny', user=self.admin, include_authors=False)
        self.assertEqual(len(results), 1)
        self.assertNotIn('email', results[0])
        review.status = 'withdrawn'
        review.save()
        self.assertEqual(_search_reviews('Archiwalny', user=self.admin, include_authors=False), [])

    def test_polish_search_preserves_accents_and_literal_percent(self):
        author = Author.objects.create(first_name="Łucja", last_name="ŻÓŁĆ", email="lc@example.test")
        matches = Author.objects.filter(pk=author.pk)
        self.assertTrue(matches.filter(last_name__plcontains="zolc").exists())
        self.assertTrue(matches.filter(first_name__plcontains="lucja").exists())
        self.assertFalse(matches.filter(last_name__plcontains="%").exists())


class HeaderSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin-ui')
        cls.member = get_user_model().objects.create_user('member-ui', email='member-ui@example.test')
        cls.member.date_joined = timezone.make_aware(datetime(2020, 6, 8, 12, 30))
        cls.member.last_login = timezone.make_aware(datetime(2026, 10, 1, 15, 45))
        cls.member.save()
        cls.person = Person.objects.create(user=cls.member, first_name='Ala', last_name='Osoba',
                                           email=cls.member.email)

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
        self.assertNotContains(self.client.get(reverse('login')), 'data-header-search')
        self.person.is_active = False
        self.person.save(update_fields=['is_active'])
        request = RequestFactory().get('/')
        request.user = self.member
        self.assertNotIn('data-header-search', render_to_string('core/base.html', request=request))


class MySQLPolishContainsTests(SimpleTestCase):
    """Produkcja działa na MySQL: zapytanie składa się bez połączenia z serwerem."""

    def test_lookup_folds_polish_l_and_uses_accent_insensitive_collation(self):
        mysql = MySQLWrapper({'ENGINE': 'django.db.backends.mysql', 'NAME': '', 'OPTIONS': {}, 'TIME_ZONE': None},
                             alias='mysql')
        query = Author.objects.filter(last_name__plcontains='ŻÓŁĆ').values('pk').query
        sql, params = query.get_compiler(connection=mysql).as_sql()
        self.assertIn('CONVERT(LOWER(REPLACE(REPLACE(`authors_author`.`last_name`, %s, %s), %s, %s)) USING utf8mb4)'
                      ' COLLATE `utf8mb4_unicode_ci` LIKE %s', sql)
        self.assertEqual(params, ('ł', 'l', 'Ł', 'L', '%zolc%'))
