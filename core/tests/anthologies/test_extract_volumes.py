"""Tomy Ekstraktów: jeden tekst na tom, flaga antologii, stopki tomów w profilach i w adminie oraz strony tomów."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.forms.models import model_to_dict
from django.test import RequestFactory, TestCase
from django.urls import reverse
from lxml import html

from core.models import PostLayoutAssignment
from core.selectors.people import profile_assignments
from core.supervision import anthology_credit_groups, integrity_issues
from fantazmaty.testing import create_member, create_superuser
from people.models import Person
from texts.admin import AnthologyAdmin, ExtractVolumeCreditInline, TextAdminForm
from texts.extract_whole import SOURCE, ensure_whole_text
from texts.models import Anthology, ExtractVolume, ExtractVolumeCredit, Text

from .support import AnthologyPageData


class WholeExtractVolumeTests(TestCase):
    def test_future_volumes_are_single_workflow_ready_for_assignment(self):
        book = Anthology.objects.create(title='Ekstrakty 4', is_extracts=True)
        text = book.texts.get()
        self.assertEqual(text.title, book.title)
        self.assertEqual(text.workflow_stages.get().stage_type, 'ready_for_editing')
        self.assertFalse(text.authors.exists())
        self.assertFalse(text.workflow_role_assignments.exists())
        self.assertIsNone(text.length)
        text.full_clean()
        book.save()
        self.assertEqual(book.texts.count(), 1)
        with self.assertRaises(ValidationError):
            Text.objects.create(title='Miniatura', length=1, anthology=book)
        # Całość tomu nie ma długości ani autora, a zwykły tekst nadal ich wymaga.
        self.assertFalse(TextAdminForm(instance=text).fields['length'].required)
        form = TextAdminForm(data=model_to_dict(text), instance=text)
        self.assertTrue(form.is_valid(), form.errors)
        with self.assertRaises(ValidationError):
            Text(title='Zwykły tekst', length=None).full_clean()
        self.client.force_login(create_member('extract-editor', 'Redaktor'))
        response = self.client.get(reverse('core:available_texts'))
        self.assertContains(response, book.title)
        for name in ('core:text_list', 'core:workflow_list'):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, book.title)
        with self.assertRaises(ValidationError):
            text.anthology = Anthology.objects.create(title='Inna książka')
            text.save(update_fields=['anthology'])
        text.refresh_from_db()
        book.title = 'Ekstrakty 4 – dopisek'
        book.save(update_fields=['title'])
        text.refresh_from_db()
        self.assertEqual(text.title, book.title)

    def test_flag_replaces_title_detection_and_excludes_novels_and_existing_texts(self):
        named = Anthology.objects.create(title='Ekstrakty jesienne bez flagi')
        self.assertFalse(Text.objects.filter(anthology=named).exists())
        self.assertIsNone(ensure_whole_text(named))
        flagged = Anthology.objects.create(title='Zbiorówka', is_extracts=True)
        whole = Text.objects.get(anthology=flagged)
        self.assertTrue(whole.is_extract_volume)
        flagged.is_novel = True
        with self.assertRaises(ValidationError):
            flagged.full_clean()
        busy = Anthology.objects.create(title='Zwykła')
        Text.objects.create(title='Zwykły tekst', anthology=busy, length=5)
        busy.is_extracts = True
        with self.assertRaises(ValidationError):
            busy.full_clean()

    def test_whole_text_is_never_added_beside_existing_texts_or_to_a_novel(self):
        busy = Anthology.objects.create(title='Zwykła')
        Text.objects.create(title='Zwykły tekst', anthology=busy, length=5)
        # Flaga zapisana z pominięciem walidacji (np. stary rekord) nie dokłada drugiego tekstu.
        Anthology.objects.filter(pk=busy.pk).update(is_extracts=True)
        self.assertIsNone(ensure_whole_text(busy))
        self.assertEqual(busy.texts.count(), 1)
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        Anthology.objects.filter(pk=novel.pk).update(is_extracts=True)
        with self.assertRaises(ValidationError):
            ensure_whole_text(novel)
        self.assertFalse(Text.objects.filter(import_source=SOURCE, import_source_row=novel.pk).exists())

    def test_moved_whole_text_is_reported_instead_of_recreated(self):
        book = Anthology.objects.create(title='Ekstrakty 5', is_extracts=True)
        whole = book.texts.get()
        other = Anthology.objects.create(title='Inna')
        Text.objects.filter(pk=whole.pk).update(anthology=other)
        with self.assertRaisesMessage(ValidationError, 'przeniesiony do innej antologii'):
            ensure_whole_text(book)
        self.assertEqual(Text.objects.filter(import_source=SOURCE, import_source_row=book.pk).count(), 1)

    def test_first_two_ready_third_available_without_fake_dates(self):
        for n in (1, 2, 3):
            book = Anthology.objects.create(title=f'Ekstrakty {n}', is_extracts=True)
            book.refresh_from_db()
            stage = book.texts.get().workflow_stages.get()
            self.assertEqual(book.status, 'ready' if n < 3 else 'in_preparation')
            self.assertEqual(stage.stage_type, 'ready' if n < 3 else 'ready_for_editing')
            self.assertIsNone(stage.started_at)
            self.assertIsNone(stage.ended_at)

    def test_extracts_texts_without_author_are_not_reported(self):
        """Błąd: kontrola spójności zgłaszała teksty tomów Ekstraktów jako „Tekst bez autora”."""
        regular = Anthology.objects.create(title='Zwykła antologia')
        Text.objects.create(title='Bez autora zwykły', anthology=regular, length=1)
        for number in (1, 2, 3):
            book = Anthology.objects.create(title=f'Ekstrakty {number}')
            Text.objects.create(title=f'Ekstrakt {number}', anthology=book, length=1)
        found = {item['detail'] for item in integrity_issues() if item['label'] == 'Tekst bez autora'}
        self.assertIn('Bez autora zwykły', found)
        self.assertFalse({'Ekstrakt 1', 'Ekstrakt 2', 'Ekstrakt 3'} & found)


class ExtractVolumeCreditTests(TestCase):
    """Stopka tomu: osoby bez konta, rola tylko dla tomów, historia w profilu bez duplikatów."""

    @classmethod
    def setUpTestData(cls):
        cls.person = Person.objects.create(first_name='Anna', last_name='Próbna', is_active=False)
        cls.volumes = []
        for number in (1, 2):
            book = Anthology.objects.create(title=f'Ekstrakty {number}')
            cls.volumes.append(book)
            ExtractVolume.objects.create(anthology=book, number=number)
        first, second = cls.volumes
        for book, role in ((first, 'Redakcja'), (first, 'Skład'), (first, 'Okładka'),
                           (second, 'Korekta poskładowa')):
            ExtractVolumeCredit.objects.create(anthology=book, person=cls.person, role=role, source_name='A.P.')
        cls.ordinary = Anthology.objects.create(title='Inna')

    def test_credit_belongs_only_to_volumes_and_has_readable_labels(self):
        credit = ExtractVolumeCredit(anthology=self.ordinary, person=self.person, role='Redakcja')
        with self.assertRaises(ValidationError) as error:
            credit.full_clean()
        self.assertIn('anthology', error.exception.message_dict)
        ExtractVolumeCredit(anthology=self.volumes[0], person=self.person, role='Korekta').full_clean()
        self.assertEqual(str(self.volumes[0].extract_volume), 'Ekstrakty 1')
        self.assertEqual(str(self.volumes[0].extract_credits.get(role='Redakcja')),
                         'Ekstrakty 1 – Redakcja: Anna Próbna')

    def test_volume_history_without_account_and_ordinary_book_unchanged(self):
        rows, summary = profile_assignments(self.person, include_authors=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(summary['completed'], 2)
        self.assertTrue(all(row['text']['authors']['all'] == [] for row in rows))
        self.assertTrue(all(row['has_completed_work'] for row in rows))
        book = self.volumes[0]
        self.assertEqual([group['label'] for group in anthology_credit_groups(book)],
                         ['Redakcja', 'Skład', 'Okładka'])
        self.assertEqual(len(anthology_credit_groups(self.ordinary)), 8)
        site_admin = AnthologyAdmin(Anthology, admin.site)
        request = RequestFactory().get('/')
        self.assertIn(ExtractVolumeCreditInline, site_admin.get_inlines(request, book))
        self.assertNotIn(ExtractVolumeCreditInline, site_admin.get_inlines(request, self.ordinary))
        self.client.force_login(create_superuser())
        response = self.client.get(reverse('core:person_detail', args=[self.person.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Brak przypisanego autora')
        self.assertContains(response, 'Ekstrakty 1')
        self.assertEqual(self.client.get(reverse('core:anthology_detail', args=[book.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin:texts_anthology_change', args=[book.pk])).status_code, 200)

    def test_post_layout_history_does_not_duplicate_volume_history(self):
        user = get_user_model().objects.create_user('proofreader', first_name='Anna', last_name='Próbna')
        self.person.user = user
        self.person.save()
        PostLayoutAssignment.objects.create(
            anthology=self.volumes[1], proofreader=user, historical=True, status='completed',
            page_from=None, page_to=None, assigned_start=None, created_by=None)
        rows, _ = profile_assignments(self.person, include_authors=True)
        self.assertEqual(len(rows), 2)

    def test_volume_records_cannot_be_added_or_deleted_in_admin(self):
        request = RequestFactory().get('/')
        request.user = create_superuser('volume-admin')
        for model in (ExtractVolume,):
            model_admin = admin.site.get_model_admin(model)
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))


class ExtractVolumePageTests(AnthologyPageData, TestCase):
    def test_extract_profile_only_text_roles_and_hidden_sections_for_volumes_one_two(self):
        person = self.member.person_profile
        permitted = ("Redakcja", "Korekta", "Weryfikacja", "Korekta poskładowa")
        for number in (1, 2, 3):
            book = Anthology.objects.create(title=f"Ekstrakty {number}")
            ExtractVolume.objects.create(anthology=book, number=number)
            for role in (*permitted, "Koordynacja działu redakcji", "Redaktor prowadzący", "Skład",
                         "Audiodeskrypcja"):
                ExtractVolumeCredit.objects.create(anthology=book, person=person, role=role)
            page = self.client.get(reverse("core:anthology_detail", args=[book.pk]))
            if number in (1, 2):
                self.assertNotContains(page, "Osoby i wykonane prace")
            else:
                self.assertContains(page, "Osoby i wykonane prace")
        rows, summary = profile_assignments(person, include_authors=True)
        self.assertEqual({r["role"] for r in rows}, set(permitted))
        self.assertEqual(len(rows), 12)
        self.assertEqual(summary["completed"], 3)
        self.assertEqual(ExtractVolumeCredit.objects.count(), 24)
        self.assertContains(self.client.get(self.url), "Osoby i wykonane prace")

    def test_extract_panels_and_length_only_change_whole_volumes(self):
        self.client.force_login(self.admin)
        normal_text = Text.objects.create(title="Testowy tekst", anthology=self.book, length=100)
        whole = ensure_whole_text(Anthology.objects.create(title="Ekstrakty 3", is_extracts=True))
        page = self.client.get(reverse("core:assigned_text_detail", args=[whole.pk]))
        doc = html.fromstring(page.content)
        headings = [x.text_content().strip() for x in doc.xpath("//h2|//summary")]
        for label in ["Recenzje tekstu", "Audiobook", "Ilustracja", "Wycofaj tekst", "Tagi i gatunek"]:
            self.assertFalse(any(label in value for value in headings), label)
        self.assertContains(page, "Notatki zespołu")
        self.assertContains(page, "nie dotyczy")
        page = self.client.get(reverse("core:text_list"), {"q": "Ekstrakty"})
        self.assertContains(page, "n.d.")
        self.assertNotContains(page, ">None<")
        normal = self.client.get(reverse("core:assigned_text_detail", args=[normal_text.pk]))
        self.assertContains(normal, "Recenzje tekstu")
        self.assertContains(normal, "Tagi i gatunek")
