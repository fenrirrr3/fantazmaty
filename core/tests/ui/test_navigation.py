"""Nawigacja: bezpieczne adresy powrotu do list, aktywna pozycja menu na stronach szczegółów,
lista recenzji bez tekstu, nawigacja Ekstraktów i zwinięte panele historii tekstu."""
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.navigation import NavigationMiddleware, list_url, origin
from illustrations.models import Illustration
from texts.models import Anthology, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage

from .support import NavigationDataMixin


class ReturnSafetyTests(SimpleTestCase):
    def test_only_local_known_list_routes_and_no_recursive_chain(self):
        request = RequestFactory().get('/')
        for value in ('https://evil.example/teksty/', '//evil.example/teksty/',
                      '/admin/', '/wyloguj/', 'javascript:alert(1)', '/not-a-route/',
                      '/teksty/\\evil', '/teksty/\n'):
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


class DetailNavigationTests(NavigationDataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.admin)

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
        self.assertContains(response, 'Informacyjny rejestr miniatur')

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
