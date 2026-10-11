"""Wielu ilustratorów jednej ilustracji: przypisanie, formularze strony i panelu admina, filtr
opublikowanych, ochrona kredytów oraz zakaz ilustracji dla rozdziałów powieści."""
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.supervision import anthology_credits
from fantazmaty.testing import create_superuser, form_value
from illustrations.editing import AssignmentForm, edit_token
from illustrations.models import Illustration, Illustrator
from texts.models import Anthology, Text


class MultipleIllustratorsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('illustration32')
        cls.book = Anthology.objects.create(title='Archiwalna antologia', status='ready',
                                            has_illustrations=True)
        cls.story = Text.objects.create(title='Opowiadanie', anthology=cls.book, length=100)
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor')
        cls.story.authors.add(cls.author)
        cls.first = Illustrator.objects.create(first_name='Anna', last_name='Rysująca')
        cls.second = Illustrator.objects.create(first_name='Jan', last_name='Ilustrujący')
        cls.illustration = Illustration.objects.create(text=cls.story)

    def setUp(self):
        self.client.force_login(self.admin)
        self.detail = reverse('illustrations:illustration_detail', args=[self.illustration.pk])
        self.list_url = reverse('illustrations:illustration_list')

    def test_multiple_assignment_and_preserved_selected_inactive_contact(self):
        self.first.is_active = False
        self.first.save()
        self.illustration.set_artists([self.first], status='assigned')
        form = AssignmentForm(
            {'illustrators': [self.first.pk, self.second.pk], 'status': 'delivered', 'version': 'test'},
            instance=self.illustration, can_assign=True)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(self.illustration.illustrators.count(), 2)
        response = self.client.get(self.detail)
        self.assertContains(response, str(self.first))
        self.assertContains(response, str(self.second))
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//input[@name="illustrators"][@checked]')), 2)
        credited = {x['name'] for x in anthology_credits(self.book) if x['role'] == 'Ilustrator'}
        self.assertEqual(credited, {str(self.first), str(self.second)})

    def test_detail_pairs_keep_four_separate_edit_forms(self):
        response = self.client.get(self.detail)
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        grid = doc.xpath('//div[@class="illustration-detail-grid"]')[0]
        self.assertEqual(grid.xpath('./section/@aria-labelledby'), [
            'illustration-assignment-heading', 'illustration-link-heading',
            'illustration-excerpt-heading', 'illustration-notes-heading'])
        self.assertEqual(grid.xpath('./section//form/input[@name="action"]/@value'),
                         ['assignment', 'link', 'excerpt', 'coordinator_notes'])
        self.assertFalse(doc.xpath('//form//form'))
        for section in grid.xpath('./section'):
            self.assertEqual(len(section.xpath('.//input[@name="csrfmiddlewaretoken"]')), 1)
            self.assertTrue(section.xpath('.//input[@name="version"]'))

    def test_published_filter_and_sort_do_not_duplicate_texts(self):
        self.illustration.set_artists([self.first, self.second], status='delivered',
                                      preserve_assignment_date=True)
        self.assertEqual(self.client.get(self.list_url).context['page_obj'].paginator.count, 0)
        response = self.client.get(self.list_url, {'hide_published': '0', 'sort': 'illustrator',
                                                   'page_size': '500'})
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertContains(response, str(self.first))
        self.assertContains(response, str(self.second))
        self.assertContains(self.client.get(self.detail), 'hide_published=0')

    def test_m2m_change_invalidates_stale_post_and_protects_credits(self):
        token = edit_token(self.admin, self.illustration)
        self.illustration.set_artists([self.first], status='delivered')
        response = self.client.post(self.detail, {'action': 'assignment', 'version': token,
                                                  'status': 'delivered', 'illustrators': [self.second.pk]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(list(self.illustration.illustrators.all()), [self.first])
        with self.assertRaises(ProtectedError), transaction.atomic():
            Illustrator.objects.filter(pk=self.first.pk).delete()
        delete_url = reverse('admin:illustrations_illustrator_delete', args=[self.first.pk])
        self.assertEqual(self.client.get(delete_url).status_code, 403)

    def test_admin_supports_multiple_artists_and_validates_empty_status(self):
        url = reverse('admin:illustrations_illustration_change', args=[self.illustration.pk])
        payload = {'text': self.story.pk, 'status': 'delivered', 'illustrators': [self.first.pk, self.second.pk],
                   '_save': 'Zapisz', '_edit_version': form_value(self.client.get(url))}
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.illustration.illustrators.count(), 2)
        payload.update(illustrators=[], _edit_version=form_value(self.client.get(url)))
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.illustration.illustrators.count(), 2)

    def test_story_management_has_no_author_or_repeat_controls(self):
        self.book.status = 'in_preparation'
        self.book.save()
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.story.pk]))
        self.assertNotContains(response, 'Pokaż kolejkę powtórzeń')
        self.assertNotContains(response, 'text-authors-management-form')
        self.assertContains(response, 'Wycofaj tekst z procesu')
        response = self.client.post(reverse('core:set_text_authors', args=[self.story.pk]), {
            'authors': [],
            '_edit_version': form_value(response),
            'workflow_token': form_value(response, name='workflow_token'),
        })
        self.assertIn(response.status_code, (403, 409))
        self.assertEqual(list(self.story.authors.all()), [self.author])

    def test_admin_and_model_reject_new_illustration_for_novel(self):
        novel = Anthology.objects.create(title="Powieść", is_novel=True)
        chapter = Text.objects.create(anthology=novel, chapter_number=1)
        request = RequestFactory().get("/")
        request.user = self.admin
        form = admin.site.get_model_admin(Illustration).get_form(request)(
            {"text": chapter.pk, "status": "unassigned"}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("text", form.errors)
        with self.assertRaises(ValidationError):
            Illustration.objects.create(text=chapter)
