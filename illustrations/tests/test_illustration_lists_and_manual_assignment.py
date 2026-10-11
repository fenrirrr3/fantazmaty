"""Ilustracje tekstów antologii: ręczne przypisanie ilustratora bez profilu, data przypisania i kolumny listy ilustracji."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from fantazmaty.testing import create_superuser
from illustrations.editing import edit_token
from illustrations.models import Illustration, Illustrator
from people.models import Person
from texts.models import Anthology, Text


class IllustrationAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin16')
        cls.artist_contact = Illustrator.objects.create(first_name='Artysta',last_name='Kontakt')
        cls.book = Anthology.objects.create(title='Nowa antologia',has_illustrations=True)
        cls.text = Text.objects.create(title='Nowy tekst',anthology=cls.book,length=20)
        cls.ill = Illustration.objects.get(text=cls.text)

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = self.settings(
            DOCUMENT_CONVERSION_DIR=Path(tmp.name) / "convert",
            ACTIVITY_SPOOL_DIR=Path(tmp.name) / "activity",
        )
        settings.enable()
        self.addCleanup(settings.disable)
        self.client.force_login(self.admin)
        self.url = reverse("illustrations:illustration_detail", args=[self.ill.pk])

    def post_illustration(self, action, **values):
        self.ill.refresh_from_db()
        return self.client.post(
            self.url, {"action": action, "version": edit_token(self.admin, self.ill), **values}
        )

    def test_manual_name_email_without_profile_and_assignment_date(self):
        before = Person.objects.count()
        accounts = get_user_model().objects.count()
        response = self.post_illustration(
            "assignment",
            manual_illustrator_name="Anna Bez Konta",
            manual_illustrator_email="anna@example.test",
            status="assigned",
        )
        self.assertEqual(response.status_code, 302)
        self.ill.refresh_from_db()
        self.assertFalse(self.ill.illustrators.exists())
        self.assertEqual(self.ill.assigned_at, timezone.localdate())
        self.assertEqual(Person.objects.count(), before)
        self.assertEqual(get_user_model().objects.count(), accounts)
        self.assertContains(
            self.client.get(reverse("illustrations:illustration_list")), "Anna Bez Konta"
        )
        old = timezone.localdate() - timedelta(days=10)
        Illustration.objects.filter(pk=self.ill.pk).update(assigned_at=old)
        response = self.post_illustration(
            "assignment",
            manual_illustrator_name="Anna Bez Konta",
            manual_illustrator_email="anna@example.test",
            status="delivered",
        )
        self.assertEqual(response.status_code, 302)
        self.ill.refresh_from_db()
        self.assertEqual(self.ill.assigned_at, old)
        response = self.post_illustration(
            "assignment", manual_illustrator_name="Inna Osoba", status="assigned"
        )
        self.assertEqual(response.status_code, 302)
        self.ill.refresh_from_db()
        self.assertEqual(self.ill.assigned_at, timezone.localdate())

    def test_manual_validation_and_switch_back_to_profile(self):
        for data in (
            {"manual_illustrator_email": "x@example.test"},
            {"manual_illustrator_name": "Anna", "manual_illustrator_email": "wrong"},
            {"manual_illustrator_name": "Anna", "illustrators": self.artist_contact.pk},
        ):
            response = self.post_illustration("assignment", status="assigned", **data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["forms"]["assignment"].errors)
            self.ill.refresh_from_db()
            self.assertEqual(self.ill.status, "unassigned")
        response = self.post_illustration(
            "assignment", manual_illustrator_name="Anna", status="assigned"
        )
        self.assertEqual(response.status_code, 302)
        response = self.post_illustration(
            "assignment", illustrators=self.artist_contact.pk, status="assigned"
        )
        self.assertEqual(response.status_code, 302)
        self.ill.refresh_from_db()
        self.assertEqual(self.ill.manual_illustrator_name, "")
        self.assertEqual(list(self.ill.illustrators.all()), [self.artist_contact])


class IllustrationListTests(TestCase):
    def test_illustration_columns_removed_only_from_list_with_sorting_intact(self):
        self.client.force_login(create_superuser())
        book = Anthology.objects.create(title='Ilustrowana antologia', has_illustrations=True)
        text = Text.objects.create(title='Tekst ilustracji', anthology=book, length=100, genre='fantasy', tags='znacznik-tagu')
        art = Illustration.objects.get(text=text)
        art.story_url = 'https://example.test/opowiadanie'
        art.illustrated_excerpt = 'Unikatowy fragment'
        art.trigger_warnings = 'Unikatowe ostrzeżenie'
        art.save()
        response = self.client.get(reverse('illustrations:illustration_list'))
        doc = html.fromstring(response.content)
        table = doc.get_element_by_id('illustrations-table')
        headers = [' '.join(node.itertext()).strip() for node in table.xpath('./thead/tr/th')]
        self.assertEqual(headers, ['Antologia', 'Tytuł tekstu', 'Autorzy', 'Ilustratorzy', 'Status', 'Data przypisania', 'Uwagi koordynatora'])
        self.assertEqual(len(table.xpath('./tbody/tr/td')), 7)
        columns = set(response.context['page_obj'].sort_columns)
        self.assertTrue(set(headers) <= columns)
        self.assertFalse(columns & {'Gatunek, tagi', 'Ostrzeżenia dotyczące treści', 'Opowiadanie', 'Ilustrowany fragment'})
        for value in (art.story_url, art.illustrated_excerpt, art.trigger_warnings, 'znacznik-tagu'):
            self.assertNotContains(response, value)
        detail = self.client.get(reverse('illustrations:illustration_detail', args=[art.pk]))
        for value in (art.story_url, art.illustrated_excerpt, art.trigger_warnings, 'znacznik-tagu'):
            self.assertContains(detail, value)
