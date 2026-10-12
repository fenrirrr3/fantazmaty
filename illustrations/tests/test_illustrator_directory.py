"""Katalog ilustratorów: dostęp i nawigacja, dodawanie i edycja kontaktów (także w panelu admina),
walidacja, konflikty wersji, dezaktywacja oraz pseudonimy."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from lxml import html

from core.odkurzacz_forms import OdkurzaczForm
from fantazmaty.testing import assign_artists, create_member, create_superuser, form_value
from illustrations.directory import edit_token
from illustrations.editing import AssignmentForm
from illustrations.models import Illustration, Illustrator
from people.models import Person, Role
from texts.models import Anthology, Text


class IllustratorDirectoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('admin-v6')
        cls.coordinator = create_member('koord-v6', 'Koordynator redakcji')
        cls.artist = create_member('artist-v6', 'Ilustrator')
        cls.editor = create_member('editor-v6', 'Redaktor')
        cls.role = Role.objects.get(name='Ilustrator')
        cls.person = Illustrator.objects.create(
            pk=cls.artist.person_profile.pk, first_name='Artysta', last_name='Kontakt',
            email=cls.artist.person_profile.email)
        cls.manual = Illustrator.objects.create(
            first_name='Anna', last_name='Rysująca', email='anna-v6@example.test',
            portfolio='https://example.org/portfolio', preferences='Fantasy i krajobrazy', covers=True)

    def setUp(self):
        activity = patch("core.activity_spool.enqueue_activity")
        activity.start()
        self.addCleanup(activity.stop)
        self.client.force_login(self.admin)
        self.list_url = reverse("illustrations:illustrator_list")
        self.add_url = reverse("illustrations:illustrator_add")
        self.edit_url = reverse("illustrations:illustrator_edit", args=[self.manual.pk])

    def token(self, url=None):
        response = self.client.get(url or self.edit_url)
        self.assertEqual(response.status_code, 200)
        return form_value(response, name="version")

    def data(self, **kwargs):
        return {
            "first_name": "Anna",
            "last_name": "Rysująca",
            "email": "anna-v6@example.test",
            "portfolio": "https://example.org/portfolio",
            "preferences": "Nowe preferencje",
            "covers": "on",
            "is_active": "on",
            **kwargs,
        }

    def test_directory_lists_accounts_and_manual_people_once_with_all_columns(self):
        before = Person.objects.count()
        response = self.client.get(self.list_url)
        for value in (
            "Imię i nazwisko",
            "Adres e-mail",
            "Portfolio",
            "Preferencje",
            "Okładki",
            "Anna Rysująca",
            "Fantasy i krajobrazy",
            self.person.email,
            "https://example.org/portfolio",
        ):
            self.assertContains(response, value)
        self.assertNotContains(response, self.editor.person_profile.email)
        self.assertEqual(response.context["page_obj"].paginator.count, 2)
        self.assertEqual(Person.objects.count(), before)

    def test_only_active_coordinators_and_superuser_can_open_directory(self):
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            for url in (self.list_url, self.add_url, self.edit_url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        Person.objects.filter(pk=self.coordinator.person_profile.pk).update(is_active=False)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_artist_editor_and_staff_cannot_access_or_post(self):
        self.editor.is_staff = True
        self.editor.save()
        self.editor.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="illustrations",
                codename__in=["view_illustrator", "change_illustrator", "add_illustrator"],
            )
        )
        for user in (self.artist, self.editor):
            self.client.force_login(user)
            for url in (self.list_url, self.add_url, self.edit_url):
                self.assertEqual(self.client.get(url).status_code, 403)
            for url in (self.add_url, self.edit_url):
                self.assertEqual(self.client.post(url, self.data()).status_code, 403)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.preferences, "Fantasy i krajobrazy")

    def test_navigation_only_for_coordinators_directly_below_illustrations(self):
        """Na stronie programów (używanej do sprawdzenia menu) etykieta Odkurzacza jest skrócona."""
        self.assertEqual(
            OdkurzaczForm().fields["normalize_formatting"].label, "Ujednolić formatowanie"
        )
        for user in (self.admin, self.coordinator, self.artist, self.editor):
            self.client.force_login(user)
            response = self.client.get(reverse("core:programs"))
            links = html.fromstring(response.content.decode()).xpath(
                '//nav[@aria-label="Główna nawigacja"]//a/@href'
            )
            if user in (self.admin, self.coordinator):
                self.assertEqual(
                    links.index(self.list_url),
                    links.index(reverse("illustrations:illustration_list")) + 1,
                )
            else:
                self.assertNotIn(self.list_url, links)
            if user == self.admin:
                self.assertContains(response, "Ujednolić formatowanie")
                self.assertNotContains(
                    response, "Ujednolić formatowanie jak przy pobieraniu ze skrzynki"
                )

    def test_manual_creation_does_not_create_account_and_can_be_assigned(self):
        accounts = get_user_model().objects.count()
        profiles = Person.objects.count()
        roles = Role.objects.count()
        self.client.force_login(self.coordinator)
        response = self.client.post(
            self.add_url, self.data(first_name="Nowa", email="new-v6@example.test")
        )
        self.assertRedirects(response, self.list_url)
        person = Illustrator.objects.get(email="new-v6@example.test")
        self.assertEqual(Person.objects.count(), profiles)
        self.assertEqual(Role.objects.count(), roles)
        self.assertFalse(any(f.is_relation for f in person._meta.fields))
        self.assertEqual(get_user_model().objects.count(), accounts)
        book = Anthology.objects.create(title="Nowa", has_illustrations=True)
        text = Text.objects.create(title="Tekst", anthology=book, length=100)
        form = AssignmentForm(instance=Illustration.objects.get(text=text), can_assign=True)
        self.assertIn(person, form.fields["illustrators"].queryset)

    def test_manual_edit_and_unchecked_cover_persist(self):
        response = self.client.post(self.edit_url, self.data(version=self.token(), covers=""))
        self.assertRedirects(response, self.list_url)
        self.manual.refresh_from_db()
        self.assertFalse(self.manual.covers)
        self.assertEqual(self.manual.preferences, "Nowe preferencje")

    def test_contact_identity_can_change_without_modifying_team_or_account(self):
        url = reverse("illustrations:illustrator_edit", args=[self.person.pk])
        original = Person.objects.get(pk=self.artist.person_profile.pk)
        before = (original.first_name, original.last_name, original.email, original.user_id)
        response = self.client.post(
            url,
            self.data(
                version=self.token(url),
                email="forged@example.test",
                user=self.admin.pk,
                is_coordinator="on",
                roles=self.role.pk,
            ),
        )
        self.assertRedirects(response, self.list_url)
        self.person.refresh_from_db()
        original.refresh_from_db()
        self.artist.refresh_from_db()
        self.assertEqual(self.person.email, "forged@example.test")
        self.assertEqual(self.person.first_name, "Anna")
        self.assertEqual(
            (original.first_name, original.last_name, original.email, original.user_id), before
        )
        self.assertNotEqual(self.artist.email, "forged@example.test")

    def test_duplicate_email_case_insensitive_does_not_create_or_attach_account(self):
        count = Illustrator.objects.count()
        response = self.client.post(self.add_url, self.data(email=self.person.email.upper()))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wpis z tym adresem e-mail już istnieje")
        self.assertEqual(Illustrator.objects.count(), count)

    def test_invalid_portfolio_email_and_blank_name_are_rejected(self):
        count = Illustrator.objects.count()
        for override in (
            {"portfolio": "javascript:alert(1)"},
            {"portfolio": "ftp://example.org/file"},
            {"email": "nie-email"},
            {"first_name": " "},
        ):
            data = {"email": "unique-v6@example.test", **override}
            response = self.client.post(self.add_url, self.data(**data))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
        self.assertEqual(Illustrator.objects.count(), count)

    def test_blank_emails_and_portfolios_are_optional(self):
        for name in ("Pierwszy", "Drugi"):
            response = self.client.post(
                self.add_url, self.data(first_name=name, email="", portfolio="")
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(Illustrator.objects.filter(email__isnull=True).count(), 2)

    def test_conflict_with_another_save_preserves_data(self):
        token = self.token()
        self.manual.preferences = "Zmiana z panelu admina"
        self.manual.save()
        response = self.client.post(self.edit_url, self.data(version=token))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Nowe preferencje", status_code=409)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.preferences, "Zmiana z panelu admina")
        self.assertEqual(self.client.post(self.edit_url, self.data()).status_code, 409)

    def test_non_illustrator_id_cannot_be_edited(self):
        url = reverse("illustrations:illustrator_edit", args=[999999])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self.data()).status_code, 404)

    def test_csrf_and_html_escaping(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(self.add_url, self.data()).status_code, 403)
        self.client.post(
            self.edit_url, self.data(version=self.token(), preferences="<script>alert(1)</script>")
        )
        self.assertContains(self.client.get(self.list_url), "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_deactivation_hides_contact_but_preserves_existing_assignments(self):
        book = Anthology.objects.create(title="Dawna praca", has_illustrations=True)
        story = Text.objects.create(title="Zachowane przypisanie", anthology=book, length=100)
        illustration = Illustration.objects.get(text=story)
        assign_artists(illustration, [self.manual], status="assigned")
        self.manual.is_active = False
        self.manual.save()
        response = self.client.get(self.list_url, {"q": "krajobrazy"})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(self.edit_url).status_code, 200)
        assignable = AssignmentForm(instance=illustration, can_assign=True).fields["illustrators"]
        self.assertIn(self.manual, assignable.queryset)
        new = AssignmentForm(instance=Illustration(), can_assign=True).fields["illustrators"]
        self.assertNotIn(self.manual, new.queryset)
        illustration.refresh_from_db()
        self.assertEqual(list(illustration.illustrators.all()), [self.manual])

    def test_admin_creation_and_edit_only_change_contact(self):
        profiles = Person.objects.count()
        accounts = get_user_model().objects.count()
        url = reverse("admin:illustrations_illustrator_add")
        page = self.client.get(url)
        self.assertContains(page, 'name="portfolio"')
        self.assertNotContains(page, 'name="roles"')
        self.assertNotContains(page, 'name="user"')
        response = self.client.post(
            url, self.data(first_name="Panel", email="panel-v6@example.test", _save="Zapisz")
        )
        self.assertEqual(response.status_code, 302)
        person = Illustrator.objects.get(email="panel-v6@example.test")
        edit_url = reverse("admin:illustrations_illustrator_change", args=[person.pk])
        token = form_value(self.client.get(edit_url))
        response = self.client.post(
            edit_url,
            self.data(first_name="Panel", email="panel-v6@example.test", _save="Zapisz",
                      _edit_version=token),
        )
        self.assertEqual(response.status_code, 302)
        person.refresh_from_db()
        self.assertTrue(person.covers)
        self.assertContains(self.client.get(self.list_url), "Panel Rysująca")
        self.assertEqual(Person.objects.count(), profiles)
        self.assertEqual(get_user_model().objects.count(), accounts)


class IllustratorPseudonymTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('pseudo-admin')
        cls.artist = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz',
                                                pseudonym='Graphos', email='artist@example.test')
        cls.book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        cls.story = Text.objects.create(title='Tekst', anthology=cls.book, length=10)
        cls.illustration = Illustration.objects.get(text=cls.story)
        assign_artists(cls.illustration, [cls.artist], status='delivered')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_inactive_email_absent_from_list_and_copy_but_available_in_edit(self):
        url = reverse('illustrations:illustrator_list')
        self.assertContains(self.client.get(url), self.artist.email)
        Illustrator.objects.filter(pk=self.artist.pk).update(is_active=False)
        for headers in ({}, {'HTTP_X_CMS_EMAIL_COPY': '1'}):
            page = self.client.get(url, {'show_inactive': '1', 'q': 'Graphos'}, **headers)
            self.assertContains(page, 'Graphos')
            self.assertNotContains(page, self.artist.email)
            self.assertContains(page, 'Przemek Świszcz (Graphos)')
            self.assertContains(page, 'Ukryty (nieaktywny)')
        edit = self.client.get(reverse('illustrations:illustrator_edit', args=[self.artist.pk]))
        self.assertContains(edit, self.artist.email)

    def test_pseudonym_in_credits_lists_picker_search_and_order(self):
        self.assertEqual(str(self.artist), 'Przemek Świszcz')
        self.assertEqual(self.illustration.illustrator_display, 'Graphos')
        other = Illustrator.objects.create(first_name='Zyta', last_name='AAA', pseudonym='Zet')
        url = reverse('illustrations:illustrator_list')
        for sort, expected in [('person', [self.artist.pk, other.pk]),
                               ('-person', [other.pk, self.artist.pk])]:
            page = self.client.get(url, {'sort': sort})
            self.assertEqual([p.pk for p in page.context['page_obj']], expected)
        found = self.client.get(url, {'q': 'graphos'}).context['page_obj']
        self.assertEqual([p.pk for p in found], [self.artist.pk])
        for url in (reverse('illustrations:illustration_list'),
                    reverse('illustrations:illustration_detail', args=[self.illustration.pk]),
                    reverse('core:assigned_text_detail', args=[self.story.pk])):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, 'Graphos')
            self.assertNotContains(page, 'Przemek Świszcz')
        admin = self.client.get(reverse('admin:illustrations_illustrator_changelist'))
        self.assertContains(admin, 'Przemek Świszcz')
        self.assertContains(admin, 'Graphos')

    def test_edit_pseudonym_and_clear_to_restore_name(self):
        url = reverse('illustrations:illustrator_edit', args=[self.artist.pk])
        for pseudonym, expected in [('  Graphos  II ', 'Graphos II'), ('', 'Przemek Świszcz')]:
            result = self.client.post(url, {
                'first_name': 'Przemek', 'last_name': 'Świszcz', 'pseudonym': pseudonym,
                'email': self.artist.email, 'is_active': 'on',
                'version': edit_token(self.admin, self.artist),
            })
            self.assertEqual(result.status_code, 302)
            self.artist.refresh_from_db()
            self.assertEqual(self.artist.display_name, expected)
        doc = html.fromstring(self.client.get(
            reverse('admin:illustrations_illustrator_change', args=[self.artist.pk])).content)
        self.assertTrue(doc.xpath('//input[@name="pseudonym"]'))
