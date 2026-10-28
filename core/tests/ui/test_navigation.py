"""Nawigacja: bezpieczne adresy powrotu do list, aktywna pozycja menu na stronach szczegółów, sekcja „Promocja”,
lista recenzji bez tekstu, nawigacja Ekstraktów i zwinięte panele historii tekstu."""
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.http import HttpResponseRedirect
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.navigation import NavigationMiddleware, list_url, origin
from fantazmaty.testing import create_member
from illustrations.models import Illustration
from people import roles
from texts.models import Anthology, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage

from .support import NavigationDataMixin


class ReturnSafetyTests(SimpleTestCase):
    def test_only_local_known_list_routes_and_no_recursive_chain(self):
        request = RequestFactory().get('/')
        for value in ('https://evil.example/teksty/', '//evil.example/teksty/',
                      '/admin/', '/wyloguj/', 'javascript:alert(1)', '/not-a-route/',
                      '/teksty/\\evil', '/teksty/\n', 'teksty/'):
            self.assertEqual(list_url(request, value), '', value)
        self.assertEqual(list_url(request, '/teksty/?q=smok&page=3&_back=/admin/'),
                         '/teksty/?q=smok&page=3')
        self.assertEqual(list_url(request, 'http://testserver/teksty/?page=2'), '/teksty/?page=2')

    def test_chapter_returns_to_novel_chapters_not_the_novel_index(self):
        request = RequestFactory().get('/teksty/12/', HTTP_REFERER='http://testserver/powiesci/2/?_back=/powiesci/')
        self.assertEqual(origin(request), '/powiesci/2/')
        request = RequestFactory().get('/powiesci/2/', HTTP_REFERER='http://testserver/powiesci/2/')
        self.assertEqual(origin(request), '')

    def test_post_inherits_detail_context_and_preserves_list_redirect(self):
        back = '/korekta-audiobookow/?q=smok&page=3&hide_completed=0'
        referer = 'http://testserver/audiobooki/5/?' + urlencode({'_back': back})
        request = RequestFactory().post('/audiobooki/5/', HTTP_REFERER=referer)
        request.navigation_return = origin(request)
        self.assertEqual(request.navigation_return, back)
        middleware = NavigationMiddleware(lambda request: None)
        result = middleware.process_response(request, HttpResponseRedirect('/korekta-audiobookow/'))
        self.assertEqual(result.url, back)
        result = middleware.process_response(request, HttpResponseRedirect('https://evil.example/audiobooki/5/'))
        self.assertEqual(result.url, 'https://evil.example/audiobooki/5/')

    def test_form_on_list_returns_to_referring_list_and_keeps_other_list_redirect(self):
        request = RequestFactory().post('/recenzje/', HTTP_REFERER='http://testserver/teksty/?page=2')
        request.navigation_return = origin(request)
        self.assertEqual(request.navigation_return, '/teksty/?page=2')
        response = NavigationMiddleware(lambda request: None).process_response(
            request, HttpResponseRedirect('/recenzje/'))
        self.assertEqual(response.url, '/recenzje/')



class DetailNavigationTests(NavigationDataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.admin)

    def test_promotion_section_only_for_social_media(self):
        """Sekcja menu „Promocja” (Social Media i kalendarz) jest tylko u superusera, koordynatora i roli
        „Social Media”; redaktor, członek bez ról, członek z samą rolą honorową i konto spoza zespołu jej nie widzą."""
        url = reverse('core:social_media')
        no_roles = create_member('nav-none', roles.EDITOR)
        no_roles.person_profile.roles.clear()
        no_roles.groups.clear()
        cases = (
            ('superuser', self.admin, True), ('koordynator', self.manager, True),
            ('social media', create_member('nav-social', roles.SOCIAL_MEDIA), True),
            ('redaktor', self.member, False), ('bez ról', no_roles, False),
            ('rola honorowa', create_member('nav-emperor', roles.EMPEROR), False),
            ('spoza zespołu', get_user_model().objects.create_user('nav-outsider'), False),
        )
        for label, user, allowed in cases:
            with self.subTest(user=label):
                self.client.force_login(user)
                doc = html.fromstring(self.client.get(reverse('core:home')).content)
                section = doc.xpath('//nav//details[@data-menu-section="promotion"]')
                self.assertEqual(bool(section), allowed)
                self.assertEqual(bool(doc.xpath(f'//nav//a[@href="{url}"]')), allowed)
                if allowed:
                    self.assertEqual(section[0].xpath('./summary/text()'), ['Promocja'])

    def test_single_add_to_review_link_active_for_merged_page_and_manual_forms(self):
        """Jeden link „Dodaj do recenzji” (scalona strona) – aktywny także po wysłaniu formularzy ręcznych."""
        page_url = reverse('core:review_bulk_import')
        cases = {
            'merged page': ('get', page_url),
            'single submission with errors': ('post', reverse('core:review_create')),
            'bulk import with errors': ('post', reverse('core:review_bulk_submit')),
        }
        for case, (method, url) in cases.items():
            with self.subTest(case):
                doc = html.fromstring(getattr(self.client, method)(url).content)
                section = doc.xpath('//nav//details[@data-menu-section="submissions"]')[0]
                self.assertEqual(section.xpath(f'.//a[@href="{page_url}"]/text()'), ['Dodaj do recenzji'])
                self.assertEqual(section.xpath('.//a[@aria-current="page"]/@href'), [page_url])
                self.assertNotIn('Pobieranie zgłoszeń', section.text_content())

    def test_detail_navigation_parents_and_audio_origin(self):
        ordinary = Anthology.objects.create(title='Ilustrowana', has_illustrations=True)
        text = Text.objects.create(title='Ilustracja nawigacyjna', anthology=ordinary, length=100)
        illustration = Illustration.objects.get(text=text)
        cases = [
            (reverse('illustrations:illustration_detail', args=[illustration.pk]), {},
             'illustrations:illustration_list'),
            (reverse('core:recruitment_detail', args=[self.applicant.pk]), {}, 'core:recruitment_list'),
            (reverse('core:unlinked_reviews'), {}, 'core:review_list'),
            (reverse('core:audiobook_detail', args=[text.pk]), {}, 'core:audiobooks'),
            (reverse('core:audiobook_detail', args=[text.pk]), {'_back': '/korekta-audiobookow/?page=2'},
             'core:audio_proofreading'),
        ]
        for url, params, expected in cases:
            with self.subTest(url=url, params=params):
                response = self.client.get(url, params)
                self.assertEqual(response.status_code, 200)
                active = html.fromstring(response.content).xpath(
                    '//a[contains(@class,"sidebar-link")][@aria-current="page"]/@href')
                self.assertEqual(active, [reverse(expected)])

    def test_unlinked_exceptions_and_extract_navigation(self):
        novel = Anthology.objects.create(title='Powieść nawigacyjna', is_novel=True)
        chapter = Text.objects.create(title='Rozdział 1', chapter_number=1, anthology=novel, length=100)
        book = Anthology.objects.create(title='Ekstrakty 3', is_extracts=True)
        whole = book.texts.get(import_source='extract-volume-v2')
        url = reverse('core:unlinked_reviews')
        def listed(**params):
            return {row['pk']: row['not_applicable'] for row in self.client.get(url, params).context['texts']}
        rows = listed()
        self.assertIn(self.text.pk, rows)
        self.assertNotIn(chapter.pk, rows)
        self.assertNotIn(whole.pk, rows)
        rows = listed(show_not_applicable='1')
        # Rozdział powieści i cały tom ekstraktów są oznaczone „Nie dotyczy”, zwykły tekst nie.
        self.assertEqual({pk for pk, not_applicable in rows.items() if not_applicable}, {chapter.pk, whole.pk})
        response = self.client.get(reverse('core:extract_list'))
        self.assertContains(response, reverse('core:assigned_text_detail', args=[whole.pk]))
        response = self.client.get(reverse('core:assigned_text_detail', args=[whole.pk]))
        self.assertContains(response, '>Lista tekstów</a>')

    def test_history_panels_are_siblings_and_initially_collapsed(self):
        Anthology.objects.create(title="Fantazmaty 4", status="ready")  # wydany tom źródłowy
        book = Anthology.objects.create(title="Fantazmaty 6")
        text = Text.objects.create(title="Opowiadanie przeniesione", anthology=book, length=100)
        assignment = WorkflowRoleAssignment.objects.create(text=text, role="editor", assigned_to=self.member)
        WorkflowStage.objects.create(text=text, stage_type="editing", assignment=assignment,
                                     started_at=timezone.localdate())
        response = self.client.get(reverse("core:assigned_text_detail", args=[text.pk]))
        doc = html.fromstring(response.content)
        panels = doc.xpath('//div[@data-detail-pair="assignment-history"]/details')
        self.assertEqual([panel.get("id") for panel in panels], ["handoff-history", "assignment-history"])
        self.assertTrue(all(panel.get("open") is None for panel in panels))
        self.assertEqual(len(doc.xpath('//*[@id="assignment-history"]')), 1)
