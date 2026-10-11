"""Strona tekstu i formularze pomocnicze: skróty do admina, notatki, rozdziały, tłumaczenia, formularz przypisań i logowanie."""
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from fantazmaty.testing import create_member, create_superuser, form_value
from texts.models import Anthology, Review, Text
from texts.novels import new_chapter
from workflow.models import WorkflowStage as S


class TextPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser()
        cls.editor = create_member('redaktor', 'Redaktor')
        cls.reviewer = create_member('recenzent', 'Recenzent')
        cls.book = Anthology.objects.create(title='Test')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)

    def test_admin_links_only_visible_to_superuser_for_both_details(self):
        today = timezone.localdate()
        S.objects.create(text=self.text, stage_type='editing', is_completed=True, started_at=today, ended_at=today)
        review = Review.objects.create(title='Recenzja', length=100, anthology=self.book,
                                       old_reviews=True)
        for kind, record, route in [('text', self.text, 'assigned_text_detail'),
                                     ('review', review, 'assigned_review_detail')]:
            with self.subTest(kind=kind):
                url = reverse('core:' + route, args=[record.pk])
                admin_url = reverse('admin:texts_' + kind + '_change', args=[record.pk])
                self.client.force_login(self.admin)
                response = self.client.get(url)
                self.assertContains(response, admin_url)
                self.assertContains(response, 'Edytuj w adminie')
                self.client.force_login(self.reviewer)
                response = self.client.get(url)
                self.assertNotContains(response, admin_url)
                self.assertNotContains(response, 'Edytuj w adminie')

    def test_notes_have_accessible_hidden_label_and_short_button(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:assigned_text_detail', args=[self.text.pk]))
        self.assertNotContains(response, '>Nowa notatka</label>')
        self.assertNotContains(response, '>Dodaj notatkę</button>')
        self.assertContains(response, '>Dodaj</button>')
        self.assertContains(response, '>Treść notatki</label>')

    def test_chapter_pairs_notes_and_reviews_without_tag_editor(self):
        self.client.force_login(self.admin)
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        chapter = new_chapter(novel, novel.novel, chapter_number=1)
        response = self.client.get(reverse('core:assigned_text_detail', args=[chapter.pk]))
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        pair = doc.xpath('//div[contains(@class,"chapter-notes-reviews")]')[0]
        self.assertTrue(pair.xpath('./section[@aria-labelledby="text-notes-heading"]'))
        self.assertTrue(pair.xpath('./details[contains(@class,"review-summary-details")]'))
        self.assertFalse(doc.xpath('//*[@id="text-tags-heading"]'))
        self.assertEqual(len(doc.xpath('//details[contains(@class,"review-summary-details")]')), 1)

    def test_original_verifier_form_save(self):
        self.client.force_login(self.admin)
        book = Anthology.objects.create(title="Przekład", is_translated=True)
        text = Text.objects.create(title="Tekst obcy", anthology=book, length=10)
        response = self.client.get(reverse("core:translation_detail", args=[text.pk]))
        self.assertContains(response, "Weryfikacja z oryginałem")
        token = form_value(response, form_xpath='//form[contains(@action,"tlumacze")]/')
        response = self.client.post(
            reverse("core:set_translators", args=[text.pk]),
            {"original_verifier": "Jan Sprawdzający", "_edit_version": token},
        )
        self.assertEqual(response.status_code, 302)
        text.translation.refresh_from_db()
        self.assertEqual(text.translation.original_verifier, "Jan Sprawdzający")

    def test_bulk_role_options_and_server_validation_match(self):
        from core.novel_forms import AssignmentForm
        form = AssignmentForm(prefix='assign')
        doc = html.fromstring(str(form))
        option = doc.xpath(f'//select[@name="assign-assignee"]/option[@value="{self.editor.pk}"]')[0]
        self.assertIn('editor', option.get('data-assignment-roles').split())
        self.assertEqual(doc.xpath('//select[@name="assign-assignee"]/@data-assignment-role-select'), ['id_assign-role'])
        invalid = AssignmentForm({'role': 'editor', 'assignee': self.reviewer.pk})
        self.assertFalse(invalid.is_valid())
        valid = AssignmentForm({'role': 'editor', 'assignee': self.editor.pk})
        self.assertTrue(valid.is_valid(), valid.errors)

    def test_take_stage_label_is_only_for_screen_readers_in_the_table(self):
        source = Path(settings.BASE_DIR) / 'core' / 'templates' / 'core' / 'includes' / 'take_stage_form.html'
        self.assertIn('{% if compact %}class="visually-hidden"', source.read_text(encoding='utf-8'))

    def test_login_uses_email_label(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Adres e-mail")
        self.assertNotContains(response, "Nazwa użytkownika")
