from urllib.parse import urlencode, urlsplit, parse_qs

from django.contrib.auth import get_user_model
from django.http import HttpResponseRedirect
from django.test import TestCase, SimpleTestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.navigation import list_url, origin, NavigationMiddleware
from core.models import AudioContributor, Recruitment
from illustrations.models import Illustrator
from texts.models import Anthology, Text, ForeignAuthor
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.tests import create_member


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


class NavigationV78Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('nav-admin', '', 'test')
        cls.manager = create_member('nav-manager', 'Koordynator redakcji')
        cls.member = create_member('nav-member', 'Redaktor')
        cls.book = Anthology.objects.create(title='Przekłady nawigacyjne', is_translated=True)
        cls.text = Text.objects.create(title='Podróż nawigacyjna', anthology=cls.book, length=100)
        cls.author = ForeignAuthor.objects.create(first_name='Secret', last_name='Name', pseudonym='Signature')
        cls.text.translation.foreign_authors.add(cls.author)
        assignment = WorkflowRoleAssignment.objects.create(text=cls.text, role='editor', assigned_to=cls.member)
        cls.stage = WorkflowStage.objects.create(text=cls.text, stage_type='editing', assignment=assignment,
                                                started_at=timezone.localdate())
        cls.audio = AudioContributor.objects.create(name='Nawigator lektor', email='secret-audio@example.test')
        cls.artist = Illustrator.objects.create(first_name='Nawigator', last_name='Plastyk', is_active=False)
        cls.applicant = Recruitment.objects.create(first_name='Nawigator', last_name='Kandydat',
                                                   email='secret-candidate@example.test')

    def test_translations_in_all_tasks_personal_and_dashboard_but_not_ordinary_workflow(self):
        self.client.force_login(self.member)
        for route in ('text_list', 'my_texts', 'home'):
            response = self.client.get(reverse('core:' + route))
            self.assertContains(response, self.text.title)
        response = self.client.get(reverse('core:dashboard_tasks'), {'kind': 'active'})
        self.assertIn(self.text.title, response.json()['html'])
        response = self.client.get(reverse('core:workflow_list'))
        self.assertNotContains(response, self.text.title)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('core:task_list'))
        self.assertContains(response, self.book.title)
        self.assertContains(response, reverse('core:anthology_detail', args=[self.book.pk]) + '#task-cover')
        response = self.client.get(reverse('core:anthology_detail', args=[self.book.pk]))
        self.assertContains(response, 'id="task-cover"')
        self.assertContains(response, 'id="task-blurb"')
        self.assertContains(response, reverse('core:translation_list') + '?anthology=')

    def test_return_through_translation_redirect_preserves_filters_and_active_menu(self):
        self.client.force_login(self.member)
        back = reverse('core:my_texts') + '?view=all&page=4&page_size=50&q=Podróż'
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]),
                                   HTTP_REFERER='http://testserver' + back, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_qs(urlsplit(response.request['PATH_INFO'] + '?' + response.request['QUERY_STRING']).query)['_back'][0],
                         '/moje-teksty/?view=all&page=4&page_size=50&q=Podr%C3%B3%C5%BC')
        doc = html.fromstring(response.content)
        active = doc.xpath('//a[contains(@class,"sidebar-link")][@aria-current="page"]/@href')
        self.assertEqual(active, [reverse('core:my_texts')])
        self.assertTrue(doc.xpath('//a[@href="' + response.context['return_url'] + '"]'))

    def test_search_expansion_preserves_permissions_and_public_pseudonym(self):
        for user, allowed in ((self.member, False), (self.manager, True)):
            self.client.force_login(user)
            response = self.client.get(reverse('core:global_search'), {'q': 'Nawigator'})
            self.assertContains(response, self.audio.name)
            check = self.assertContains if allowed else self.assertNotContains
            check(response, self.applicant.full_name)
            check(response, str(self.artist))
            self.assertNotContains(response, 'secret-audio@example.test')
            self.assertNotContains(response, 'secret-candidate@example.test')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:global_search'), {'q': 'Signature'})
        self.assertContains(response, self.text.title)
        self.assertContains(response, reverse('core:translation_detail', args=[self.text.pk]))
        self.assertNotContains(response, 'Secret Name')

    def test_unlinked_exceptions_and_extract_navigation(self):
        novel = Anthology.objects.create(title='Powieść nawigacyjna', is_novel=True)
        chapter = Text.objects.create(title='Rozdział 1', chapter_number=1, anthology=novel, length=100)
        book = Anthology.objects.create(title='Ekstrakty 3', is_extracts=True)
        whole = book.texts.get(import_source='extract-volume-v2')
        self.client.force_login(self.admin)
        url = reverse('core:unlinked_reviews')
        response = self.client.get(url)
        self.assertContains(response, self.text.title)
        self.assertNotContains(response, chapter.title)
        self.assertNotContains(response, whole.title)
        response = self.client.get(url, {'show_not_applicable': '1'})
        self.assertContains(response, chapter.title)
        self.assertContains(response, whole.title)
        self.assertContains(response, 'Nie dotyczy', count=2)
        response = self.client.get(reverse('core:extract_list'))
        self.assertContains(response, reverse('core:assigned_text_detail', args=[whole.pk]))
        response = self.client.get(reverse('core:assigned_text_detail', args=[whole.pk]))
        self.assertContains(response, 'Informacyjny rejestr miniatur')

    def test_task_links_reach_visible_novel_form_and_ad_forms_warn_separately(self):
        self.client.force_login(self.manager)
        novel = Anthology.objects.create(title='Zadania powieści', is_novel=True)
        response = self.client.get(reverse('core:novel_detail', args=[novel.pk]), {'tasks': '1'})
        doc = html.fromstring(response.content)
        self.assertTrue(doc.xpath('//details[@id="novel-production"][@open]'))
        self.assertTrue(doc.xpath('//*[@id="task-blurb"]'))
        response = self.client.get(reverse('core:audio_description_detail', args=[self.book.pk]))
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//form[@data-warn-unsaved]')), 4)
        self.assertTrue(doc.xpath('//form[@data-autosave]/input[@name="action"][@value="content"]'))

    def test_detail_navigation_parents_and_audio_origin(self):
        self.client.force_login(self.admin)
        ordinary = Anthology.objects.create(title='Ilustrowana', has_illustrations=True)
        text = Text.objects.create(title='Ilustracja nawigacyjna', anthology=ordinary, length=100)
        from illustrations.models import Illustration
        illustration = Illustration.objects.get(text=text)
        cases = [
            (reverse('illustrations:illustration_detail', args=[illustration.pk]), {}, 'illustrations:illustration_list'),
            (reverse('core:recruitment_detail', args=[self.applicant.pk]), {}, 'core:recruitment_list'),
            (reverse('core:unlinked_reviews'), {}, 'core:review_list'),
            (reverse('core:audiobook_detail', args=[text.pk]), {}, 'core:audiobooks'),
            (reverse('core:audiobook_detail', args=[text.pk]), {'_back': '/korekta-audiobookow/?page=2'}, 'core:audio_proofreading'),
        ]
        for url, params, expected in cases:
            with self.subTest(url=url, params=params):
                response = self.client.get(url, params)
                self.assertEqual(response.status_code, 200)
                active = html.fromstring(response.content).xpath('//a[contains(@class,"sidebar-link")][@aria-current="page"]/@href')
                self.assertEqual(active, [reverse(expected)])

    def test_coordinator_saves_translated_book_tasks_without_admin_and_returns_to_filtered_tasks(self):
        self.client.force_login(self.manager)
        url = reverse('core:anthology_detail', args=[self.book.pk])
        back = '/zadania/?q=Przekłady&page=2'
        response = self.client.get(url, {'_back': back})
        doc = html.fromstring(response.content)
        data = {'_edit_version': doc.xpath('//input[@name="_edit_version"]/@value')[0], '_back': back}
        for form in response.context['task_forms']:
            data[form.prefix + '-status'] = form['status'].value()
            data[form.prefix + '-assigned_to'] = form['assigned_to'].value() or ''
        data.update({'blurb-status': 'commissioned', 'blurb-assigned_to': self.member.person_profile.pk})
        saved = self.client.post(url, data)
        self.assertEqual(saved.status_code, 302)
        self.assertIn('_back=', saved.url)
        task = self.book.production_tasks.get(task_type='blurb')
        self.assertEqual(task.status, 'commissioned')
        self.assertEqual(task.assigned_to_id, self.member.person_profile.pk)
        self.assertEqual(task.commissioned_at, timezone.localdate())
        self.assertNotIn('Brak autora', str(response.context['issues']))


class FormSafetyTests(SimpleTestCase):
    def test_unsaved_forms_and_draft_restore(self):
        from pathlib import Path
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('Test JavaScript wymaga Node.js.')
        script = Path(__file__).with_name('js_tests') / 'form_safety.cjs'
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
