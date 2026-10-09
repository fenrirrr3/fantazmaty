from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from lxml import html

from illustrations.models import Illustration, Illustrator
from texts.models import Anthology, NovelProfile, Text
from texts.novels import new_chapter


class DetailInterfaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('ui34', 'ui34@example.test', 'test')
        cls.active = Illustrator.objects.create(first_name='Anna', last_name='Aktywna')
        cls.inactive = Illustrator.objects.create(first_name='Jan', last_name='Nieaktywny', is_active=False)
        cls.book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        cls.story = Text.objects.create(title='Tekst', anthology=cls.book, length=100)
        cls.novel = Anthology.objects.create(title='Powieść', is_novel=True)
        profile, _ = NovelProfile.objects.get_or_create(anthology=cls.novel)
        cls.chapter = new_chapter(cls.novel, profile, chapter_number=1)

    def setUp(self):
        activity = patch('core.activity_spool.enqueue_activity')
        activity.start()
        self.addCleanup(activity.stop)
        self.client.force_login(self.admin)

    def test_inactive_filter_is_opt_in_and_works_with_search(self):
        url = reverse('illustrations:illustrator_list')
        response = self.client.get(url)
        self.assertEqual(list(response.context['page_obj']), [self.active])
        self.assertFalse(html.fromstring(response.content).xpath('//input[@name="show_inactive"][@checked]'))
        response = self.client.get(url, {'show_inactive': '1'})
        self.assertEqual(set(response.context['page_obj']), {self.active, self.inactive})
        self.assertContains(response, '(nieaktywny)')
        self.assertTrue(html.fromstring(response.content).xpath('//input[@name="show_inactive"][@checked]'))
        response = self.client.get(url, {'show_inactive': '1', 'q': 'Nieaktywny'})
        self.assertEqual(list(response.context['page_obj']), [self.inactive])

    def test_pagination_preserves_inactive_filter(self):
        Illustrator.objects.bulk_create([Illustrator(first_name='Kontakt', last_name=str(i)) for i in range(30)])
        response = self.client.get(reverse('illustrations:illustrator_list'), {'show_inactive': '1', 'page_size': '25'})
        links = html.fromstring(response.content).xpath('//a[contains(@href,"page=2")]/@href')
        self.assertTrue(links)
        self.assertTrue(all('show_inactive=1' in link for link in links))

    def test_story_and_chapter_have_paired_collapsed_panels(self):
        for story in (self.story, self.chapter):
            with self.subTest(story=story.title):
                response = self.client.get(reverse('core:assigned_text_detail', args=[story.pk]))
                self.assertEqual(response.status_code, 200)
                doc = html.fromstring(response.content)
                for pair, expected in [('credits-history', ['text-credits', 'text-stage-history']),
                                       ('management', ['text-withdraw', 'text-handoff'])]:
                    panels = doc.xpath(f'//div[@data-detail-pair="{pair}"]/details')
                    self.assertEqual([panel.get('id') for panel in panels], expected)
                    self.assertTrue(all('open' not in panel.attrib for panel in panels))
                self.assertNotContains(response, 'Zarządzanie tekstem')
                self.assertEqual(doc.xpath('normalize-space(//*[@id="text-withdraw"]/summary/span[1])'), 'Wycofaj tekst')
                self.assertEqual(doc.xpath('normalize-space(//*[@id="text-credits"]/summary/span[1])'), 'Stopka tekstu')
                if story == self.chapter:
                    self.assertFalse(doc.xpath('//*[@id="text-audiobook"]'))

    def test_picker_retains_existing_inactive_selection_without_exposing_full_list(self):
        illustration = Illustration.objects.get(text=self.story)
        illustration.set_artists([self.active, self.inactive], status='assigned')
        response = self.client.get(reverse('illustrations:illustration_detail', args=[illustration.pk]))
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//*[@id="illustrator-source"][@hidden]//input[@checked]')), 2)
        self.assertTrue(doc.xpath('//*[@id="illustrator-search-results"][@hidden]'))
        self.assertFalse(doc.xpath('//select[@name="illustrators"]'))
        self.assertContains(response, 'illustrator_search.js')
