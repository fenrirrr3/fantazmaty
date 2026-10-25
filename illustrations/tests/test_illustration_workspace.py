"""Przestrzeń robocza ilustracji: widoczność, metadane tekstu, przypisanie i daty, link i fragment,
konflikty wersji, uprawnienia ilustratorów i redaktorów oraz kolumny listy ilustracji."""
from datetime import timedelta
from tempfile import TemporaryDirectory

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.permissions import can_view_illustrations
from fantazmaty.testing import assign_artists, create_member, create_superuser
from people.models import Role
from illustrations.editing import edit_token
from illustrations.models import Illustration, Illustrator, PublicIllustrationSettings
from texts.models import Anthology, Review, Text


class IllustrationWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser("admin-v5")
        cls.artist = create_member("ilustrator-v5", "Ilustrator")
        cls.other = create_member("inny-ilustrator-v5", "Ilustrator")
        cls.editor = create_member("redaktor-v5", "Redaktor")
        cls.artist_contact = Illustrator.objects.create(first_name="Artysta", last_name="Pierwszy")
        cls.other_contact = Illustrator.objects.create(first_name="Artysta", last_name="Drugi")
        cls.book = Anthology.objects.create(title="Ilustrowana", has_illustrations=True)
        cls.text = Text.objects.create(
            title="Tytuł ilustracji",
            anthology=cls.book,
            length=100,
            genre="Science fantasy",
            content_warnings="Ostrzeżenia tekstu",
        )
        cls.text.authors.add(Author.objects.create(first_name="Jan", last_name="Autor"))
        cls.illustration = Illustration.objects.get(text=cls.text)
        cls.review = Review.objects.create(
            title=cls.text.title,
            anthology=cls.book,
            length=100,
            email="autor@example.test",
            author_first_name="Jan",
            author_last_name="Autor",
            genre="Science fantasy",
            copied_text=cls.text,
        )

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(self.admin)
        self.url = reverse("illustrations:illustration_detail", args=[self.illustration.pk])

    def token(self, client=None, action="assignment"):
        response = (client or self.client).get(self.url)
        self.assertEqual(response.status_code, 200)
        return html.fromstring(response.content).xpath(
            '//form[input[@name="action"][@value=$action]]/input[@name="version"]/@value',
            action=action,
        )[0]

    def post(self, action, **data):
        return self.client.post(
            self.url, {"action": action, "version": self.token(action=action), **data}
        )

    def assigned(self):
        self.illustration.illustrators.set([self.artist_contact])
        Illustration.objects.filter(pk=self.illustration.pk).update(
            status="assigned", assigned_at=timezone.localdate() - timedelta(days=20)
        )

    def test_list_keeps_summary_and_detail_shows_only_text_warnings(self):
        """Lista pokazuje podsumowanie; szczegóły – gatunek i ostrzeżenia z tekstu (puste – „Nie podano”)."""
        response = self.client.get(reverse("illustrations:illustration_list"))
        for value in ("Ilustrowana", "Jan Autor", self.text.title):
            self.assertContains(response, value)
        for value in ("Science fantasy", "Ostrzeżenia tekstu"):
            self.assertNotContains(response, value)
        detail = self.client.get(self.url)
        for value in ("Science fantasy", "Ostrzeżenia tekstu"):
            self.assertContains(detail, value)
        Text.objects.filter(pk=self.text.pk).update(content_warnings="")
        detail = self.client.get(self.url)
        self.assertNotContains(detail, "Ostrzeżenia tekstu")
        self.assertContains(detail, "Nie podano")

    def test_only_in_preparation_and_illustrated_anthologies_are_visible(self):
        for field, value in (("status", "ready"), ("has_illustrations", False)):
            with self.subTest(field=field):
                Anthology.objects.filter(pk=self.book.pk).update(**{field: value})
                self.assertNotContains(
                    self.client.get(reverse("illustrations:illustration_list")), self.text.title
                )
                self.assertEqual(
                    self.client.get(self.url).status_code, 200 if field == "status" else 404
                )
                Anthology.objects.filter(pk=self.book.pk).update(
                    status="in_preparation", has_illustrations=True
                )

    def test_enabling_illustrations_creates_entry_with_tags_and_disabling_keeps_its_data(self):
        """Włączenie ilustracji w istniejącej antologii tworzy brakujący wpis (z tagami tekstu
        i plakietką statusu), a wyłączenie ukrywa go bez kasowania danych."""
        book = Anthology.objects.create(title="Antologia audytowa")
        story = Text.objects.create(title="Opowiadanie", anthology=book, length=100)
        book.has_illustrations = True
        book.save()
        story.genre = "fantasy"
        story.tags = "smoki, magia"
        story.save()
        illustration = Illustration.objects.get(text=story)
        response = self.client.get(
            reverse("illustrations:illustration_detail", args=[illustration.pk])
        )
        self.assertContains(response, "smoki, magia")
        self.assertContains(response, "illustration-status-unassigned")
        book.has_illustrations = False
        book.save()
        illustration.coordinator_notes = "Zachowane"
        illustration.save()
        self.assertEqual(
            Illustration.objects.get(pk=illustration.pk).coordinator_notes, "Zachowane"
        )

    def test_saves_assignment_status_and_date(self):
        response = self.post("assignment", illustrators=self.artist_contact.pk, status="assigned")
        self.assertEqual(response.status_code, 302)
        self.illustration.refresh_from_db()
        self.assertEqual(list(self.illustration.illustrators.all()), [self.artist_contact])
        self.assertEqual(self.illustration.assigned_at, timezone.localdate())
        response = self.post("assignment", illustrators=self.artist_contact.pk, status="delivered")
        self.assertEqual(response.status_code, 302)

    def test_corrections_do_not_reset_date_but_new_performer_does(self):
        self.assigned()
        original = timezone.localdate() - timedelta(days=20)
        for status in ("delivered", "in_corrections", "assigned"):
            response = self.post("assignment", illustrators=self.artist_contact.pk, status=status)
            self.assertEqual(response.status_code, 302)
            self.illustration.refresh_from_db()
            self.assertEqual(self.illustration.assigned_at, original)
        self.post("assignment", illustrators=self.other_contact.pk, status="assigned")
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.assigned_at, timezone.localdate())

    def test_unassignment_clears_date_and_missing_illustrator_is_rejected(self):
        self.assertEqual(
            self.post("assignment", status="assigned", illustrators=[]).status_code, 200
        )
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.status, "unassigned")
        self.assigned()
        self.assertEqual(
            self.post("assignment", status="unassigned", illustrators=[]).status_code, 302
        )
        self.illustration.refresh_from_db()
        self.assertIsNone(self.illustration.assigned_at)

    def test_link_and_long_excerpt_save_separately_without_changing_assignment(self):
        self.assigned()
        self.assertEqual(
            self.post("link", story_url="https://example.org/opowiadanie?dl=1").status_code, 302
        )
        fragment = "Długi fragment z polskimi znakami.\n" * 500
        self.assertEqual(self.post("excerpt", illustrated_excerpt=fragment).status_code, 302)
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.illustrated_excerpt, fragment.strip())
        self.assertEqual(self.illustration.story_url, "https://example.org/opowiadanie?dl=1")
        self.assertEqual(self.illustration.status, "assigned")
        self.assertEqual(self.illustration.assigned_at, timezone.localdate() - timedelta(days=20))
        self.assertNotContains(self.client.get(self.url), "Wybierz folder Dropbox")

    def test_invalid_link_and_html_excerpt_are_safe(self):
        response = self.post("link", story_url="javascript:alert(1)")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'href="javascript:')
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.story_url, "")
        self.post("excerpt", illustrated_excerpt="<script>alert(1)</script>")
        self.assertContains(self.client.get(self.url), "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_old_or_missing_token_does_not_overwrite_newer_data(self):
        old = self.token(action="link")
        self.illustration.story_url = "https://example.org/new"
        self.illustration.save()
        response = self.client.post(
            self.url, {"action": "link", "version": old, "story_url": "https://example.org/old"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "https://example.org/old", status_code=409)
        self.assertContains(response, 'href="https://example.org/new"', status_code=409)
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.story_url, "https://example.org/new")
        response = self.client.post(
            self.url, {"action": "excerpt", "illustrated_excerpt": "brak tokena"}
        )
        self.assertEqual(response.status_code, 409)

    def test_artist_account_does_not_grant_contact_edit_rights(self):
        self.assigned()
        for user in (self.artist, self.other):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(self.url), "Zapisz fragment")
            response = self.client.post(
                self.url, {"action": "excerpt", "illustrated_excerpt": "Obcy fragment"}
            )
            self.assertEqual(response.status_code, 403)
            response = self.client.post(self.url, {
                "action": "assignment", "illustrators": self.other_contact.pk, "status": "delivered",
            })
            self.assertEqual(response.status_code, 403)
        self.illustration.refresh_from_db()
        self.assertEqual(list(self.illustration.illustrators.all()), [self.artist_contact])
        self.assertEqual(self.illustration.status, "assigned")

    def test_editor_without_illustration_access_and_csrf_are_blocked(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("illustrations:illustration_list")).status_code, 403
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        response = csrf_client.post(self.url, {"action": "excerpt", "illustrated_excerpt": "Nie"})
        self.assertEqual(response.status_code, 403)


class IllustrationListTests(TestCase):
    def test_illustration_columns_removed_only_from_list_with_sorting_intact(self):
        self.client.force_login(create_superuser())
        book = Anthology.objects.create(title='Ilustrowana antologia', has_illustrations=True)
        text = Text.objects.create(title='Tekst ilustracji', anthology=book, length=100, genre='fantasy', tags='znacznik-tagu')
        art = Illustration.objects.get(text=text)
        art.story_url = 'https://example.test/opowiadanie'
        art.illustrated_excerpt = 'Unikatowy fragment'
        art.save()
        Text.objects.filter(pk=text.pk).update(content_warnings='Unikatowe ostrzeżenie')
        response = self.client.get(reverse('illustrations:illustration_list'))
        doc = html.fromstring(response.content)
        table = doc.get_element_by_id('illustrations-table')
        headers = [' '.join(node.itertext()).strip() for node in table.xpath('./thead/tr/th')]
        self.assertEqual(headers, ['Antologia', 'Tytuł tekstu', 'Autorzy', 'Ilustratorzy', 'Status', 'Data przypisania', 'Uwagi koordynatora'])
        self.assertEqual(len(table.xpath('./tbody/tr/td')), 7)
        columns = set(response.context['page_obj'].sort_columns)
        self.assertTrue(set(headers) <= columns)
        self.assertFalse(columns & {'Gatunek, tagi', 'Ostrzeżenia dotyczące treści', 'Opowiadanie', 'Ilustrowany fragment'})
        for value in (art.story_url, art.illustrated_excerpt, 'Unikatowe ostrzeżenie', 'znacznik-tagu'):
            self.assertNotContains(response, value)
        detail = self.client.get(reverse('illustrations:illustration_detail', args=[art.pk]))
        for value in (art.story_url, art.illustrated_excerpt, 'Unikatowe ostrzeżenie', 'znacznik-tagu'):
            self.assertContains(detail, value)


class IllustrationListFiltersAndRulesTests(TestCase):
    """Filtry i sortowanie listy, dostęp spoza zespołu, nieznana operacja zapisu i reguły modelu."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('filters-admin')
        cls.contact = Illustrator.objects.create(first_name='Artysta', last_name='Filtrowy')
        cls.book = Anthology.objects.create(title='Antologia B', has_illustrations=True)
        cls.other_book = Anthology.objects.create(title='Antologia A', has_illustrations=True)
        cls.assigned = Text.objects.create(title='Przypisany', anthology=cls.book, length=1, genre='groza', tags='las')
        cls.free = Text.objects.create(title='Wolny', anthology=cls.other_book, length=1)
        assign_artists(Illustration.objects.get(text=cls.assigned), [cls.contact], status='assigned')
        cls.list_url = reverse('illustrations:illustration_list')

    def setUp(self):
        self.client.force_login(self.admin)

    def titles(self, **params):
        response = self.client.get(self.list_url, params)
        self.assertEqual(response.status_code, 200)
        return [row.text.title for row in response.context['page_obj']]

    def test_status_anthology_and_sort_filters_with_invalid_values_ignored(self):
        self.assertEqual(self.titles(status='assigned'), ['Przypisany'])
        self.assertEqual(self.titles(status='bogus'), ['Wolny', 'Przypisany'])
        self.assertEqual(self.titles(anthology=str(self.book.pk)), ['Przypisany'])
        for invalid in ('abc', '0', '-1', '9' * 20, '٣'):
            with self.subTest(anthology=invalid):
                self.assertEqual(self.titles(anthology=invalid), ['Wolny', 'Przypisany'])
        self.assertEqual(self.titles(sort='-title'), ['Wolny', 'Przypisany'])
        # Status sortuje się po etykiecie („Nieprzypisane” < „Przypisane”).
        self.assertEqual(self.titles(sort='-status'), ['Przypisany', 'Wolny'])
        self.assertEqual(self.titles(sort='status'), ['Wolny', 'Przypisany'])
        self.assertEqual(self.titles(sort='password'), self.titles())

    def test_unknown_save_operation_is_rejected_without_changes(self):
        illustration = Illustration.objects.get(text=self.free)
        url = reverse('illustrations:illustration_detail', args=[illustration.pk])
        for action in ('delete', ''):
            response = self.client.post(url, {'action': action, 'version': edit_token(self.admin, illustration),
                                              'status': 'assigned'})
            self.assertEqual(response.status_code, 400)
        illustration.refresh_from_db()
        self.assertEqual(illustration.status, 'unassigned')

    def test_model_rules_for_status_date_and_display(self):
        illustration = Illustration.objects.get(text=self.assigned)
        self.assertEqual((illustration.anthology, illustration.genre_tags_display), (self.book, 'groza · las'))
        illustration.status = 'unassigned'
        with self.assertRaisesMessage(ValidationError, 'zmień status'):
            illustration.save()
        # Zapis wybranych pól nadal czyści datę przypisania po odpięciu ilustratorów.
        illustration.illustrators.clear()
        illustration.save(update_fields=['status'])
        illustration.refresh_from_db()
        self.assertEqual((illustration.status, illustration.assigned_at), ('unassigned', None))
        self.assertEqual(str(PublicIllustrationSettings()), 'Zewnętrzne ilustracje – GDrive i wytyczne')


class LegacyIllustrationTests(TestCase):
    """Stare (importowane) antologie ilustrowane, już Gotowe: sygnały nie zakładają wpisów, więc tekst bez wpisu
    dostaje go z podglądu tekstu, a niespójny wpis („Oddane” bez ilustratorki) da się uzupełnić – koordynatorowi
    i superuserowi; ilustrator i redaktor nadal nie mogą."""

    @classmethod
    def setUpTestData(cls):
        cls.users = {'superuser': create_superuser('legacy-admin'),
                     'koordynator': create_member('legacy-coordinator', 'Koordynator ilustracji'),
                     'ilustrator': create_member('legacy-artist', 'Ilustrator'),
                     'redaktor': create_member('legacy-editor', 'Redaktor')}
        cls.book = Anthology.objects.create(title='Stara ilustrowana', status='ready', has_illustrations=True)
        cls.plain = Anthology.objects.create(title='Stara bez ilustracji', status='ready')
        cls.without_entry = Text.objects.create(title='Bez wpisu', anthology=cls.book, length=1)
        cls.not_illustrated = Text.objects.create(title='Nieilustrowany', anthology=cls.plain, length=1)
        legacy_text = Text.objects.create(title='Oddane bez ilustratorki', anthology=cls.book, length=1)
        # Import pomijał walidację modelu: status „Oddane” bez przypisanej osoby.
        Illustration.objects.bulk_create([Illustration(text=legacy_text, status='delivered')])
        cls.legacy = Illustration.objects.get(text=legacy_text)

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=temporary.name)
        override.enable()
        self.addCleanup(override.disable)

    def test_missing_entry_is_created_from_text_page_only_by_coordinators(self):
        """Gotowa antologia nie dostaje wpisów automatycznie; podgląd tekstu pokazuje koordynatorowi i superuserowi
        „Utwórz wpis ilustracji”, POST zakłada wpis (bez duplikatu przy powtórzeniu) i otwiera go; ilustrator
        i redaktor – bez przycisku i 403; tekst z nieilustrowanej antologii – odmowa bez wpisu."""
        self.assertFalse(Illustration.objects.filter(text=self.without_entry).exists())
        page = reverse('core:assigned_text_detail', args=[self.without_entry.pk])
        create = reverse('illustrations:create_illustration', args=[self.without_entry.pk])
        for name, allowed in (('ilustrator', False), ('redaktor', False), ('superuser', True), ('koordynator', True)):
            with self.subTest(user=name):
                self.client.force_login(self.users[name])
                response = self.client.get(page)
                if response.status_code == 200:
                    self.assertEqual('Utwórz wpis ilustracji' in response.content.decode(), allowed)
                response = self.client.post(create)
                if not allowed:
                    self.assertEqual(response.status_code, 403)
                    self.assertFalse(Illustration.objects.filter(text=self.without_entry).exists())
                    continue
                illustration = Illustration.objects.get(text=self.without_entry)
                detail = reverse('illustrations:illustration_detail', args=[illustration.pk])
                self.assertRedirects(response, detail)
                self.assertEqual(illustration.status, 'unassigned')
                self.assertRedirects(self.client.post(create), detail)
                self.assertEqual(Illustration.objects.filter(text=self.without_entry).count(), 1)
                self.assertContains(self.client.get(page), f'href="{detail}"')
                illustration.delete()
        response = self.client.post(reverse('illustrations:create_illustration', args=[self.not_illustrated.pk]),
                                    follow=True)
        self.assertContains(response, 'Ten tekst nie może mieć ilustracji')
        self.assertFalse(Illustration.objects.filter(text=self.not_illustrated).exists())

    def test_legacy_entry_without_illustrator_is_completed_by_coordinators(self):
        """Wpis „Oddane” bez ilustratorki: koordynator i superuser zapisują link (wcześniej błąd 500) i dopisują
        brakującą ilustratorkę bez zmiany statusu; ilustrator i redaktor dostają 403 bez zmian; zmiana samego
        statusu bez ilustratora nadal jest odrzucana."""
        url = reverse('illustrations:illustration_detail', args=[self.legacy.pk])
        for name in ('ilustrator', 'redaktor'):
            with self.subTest(user=name):
                self.client.force_login(self.users[name])
                response = self.client.post(url, {'action': 'link', 'version': edit_token(self.users[name], self.legacy),
                                                  'story_url': 'https://example.org/obce'})
                self.assertEqual(response.status_code, 403)
        for number, name in enumerate(('superuser', 'koordynator')):
            with self.subTest(user=name):
                user = self.users[name]
                self.client.force_login(user)
                Illustration.objects.filter(pk=self.legacy.pk).update(status='delivered', story_url='')
                self.legacy.illustrators.clear()
                self.assertContains(self.client.get(url), 'Zapisz przypisanie i status')
                response = self.client.post(url, {'action': 'link', 'version': edit_token(user, self.legacy),
                                                  'story_url': 'https://example.org/stare'})
                self.assertEqual(response.status_code, 302)
                self.legacy.refresh_from_db()
                response = self.client.post(url, {
                    'action': 'assignment', 'version': edit_token(user, self.legacy), 'status': 'delivered',
                    'new_illustrator_name': f'Anna Ilustratorka{number}'})
                self.assertEqual(response.status_code, 302)
                self.legacy.refresh_from_db()
                self.assertEqual((self.legacy.story_url, self.legacy.status, self.legacy.illustrator_display),
                                 ('https://example.org/stare', 'delivered', f'Anna Ilustratorka{number}'))
        self.legacy.illustrators.clear()
        self.legacy.status = 'assigned'
        with self.assertRaisesMessage(ValidationError, 'wymaga wybrania ilustratora'):
            self.legacy.save()

    def test_create_refuses_texts_outside_illustration_production(self):
        """„Utwórz wpis ilustracji” przez adres dla tekstu, który nie może mieć ilustracji mimo ilustrowanej
        antologii (porzucona antologia, powieść, tłumaczenie, tekst wycofany): odmowa z komunikatem, bez wpisu
        i bez przycisku w podglądzie."""
        from workflow.models import WorkflowStage
        abandoned = Anthology.objects.create(title='Porzucona ilustrowana', status='abandoned', has_illustrations=True)
        novel = Anthology.objects.create(title='Powieść ilustrowana', is_novel=True, has_illustrations=True)
        translated = Anthology.objects.create(title='Tłumaczona ilustrowana', is_translated=True, has_illustrations=True)
        withdrawn = Text.objects.create(title='Wycofany', anthology=self.book, length=1)
        WorkflowStage.objects.create(text=withdrawn, stage_type='withdrawn', started_at=timezone.localdate())
        cases = {
            'porzucona antologia': Text.objects.create(title='W porzuconej', anthology=abandoned, length=1),
            'powieść': Text.objects.create(title='Rozdział', anthology=novel, chapter_number=1, length=1),
            'tłumaczenie': Text.objects.create(title='Przekład', anthology=translated, length=1),
            'tekst wycofany': withdrawn,
        }
        self.client.force_login(self.users['superuser'])
        for case, text in cases.items():
            with self.subTest(case):
                response = self.client.post(reverse('illustrations:create_illustration', args=[text.pk]), follow=True)
                self.assertContains(response, 'Ten tekst nie może mieć ilustracji')
                self.assertNotContains(response, 'Utwórz wpis ilustracji')
                self.assertFalse(Illustration.objects.filter(text=text).exists())


class IllustratorAccessTests(TestCase):
    """Osoba z rolą Ilustrator i inną rolą zespołu zachowuje dostęp do Ilustracji."""

    def test_illustrator_with_another_role_keeps_illustrations_access(self):
        user = create_member('ilustratorka', 'Redaktor')
        user.person_profile.roles.add(Role.objects.get_or_create(name='Ilustrator')[0])
        self.assertTrue(can_view_illustrations(user))
        self.client.force_login(user)
        response = self.client.get(reverse('core:home'))
        self.assertContains(response, reverse('illustrations:illustration_list'))
        self.assertEqual(self.client.get(reverse('illustrations:illustration_list')).status_code, 200)


class IllustrationAdminTests(TestCase):
    """Panel admina ilustracji: kolumny listy i walidacja linku do opowiadania w formularzu."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('illustration-gaps')
        cls.book = Anthology.objects.create(title='Ilustrowana', has_illustrations=True)
        cls.text = Text.objects.create(title='Z antologii', anthology=cls.book, length=10)
        cls.illustration = Illustration.objects.get(text=cls.text)
        cls.loose = Illustration.objects.create(text=Text.objects.create(title='Bez antologii', length=10))

    def setUp(self):
        self.client.force_login(self.admin)

    def test_list_shows_title_anthology_status_and_missing_illustrator(self):
        response = self.client.get(reverse('admin:illustrations_illustration_changelist'))
        model_admin = response.context['cl'].model_admin
        rows = {model_admin.display_title(row): row for row in response.context['cl'].result_list}
        self.assertEqual({title: model_admin.display_anthology(row) for title, row in rows.items()},
                         {'Z antologii': 'Ilustrowana', 'Bez antologii': '–'})
        self.assertEqual(model_admin.display_illustrator(rows['Z antologii']), 'Nie przypisano')
        self.assertContains(response, '<span class="illustration-status illustration-status-unassigned">Nieprzypisane</span>',
                            html=True)

    def test_change_form_validates_story_link(self):
        url = reverse('admin:illustrations_illustration_change', args=[self.illustration.pk])
        page = self.client.get(url)
        data = {'status': 'unassigned', 'story_url': 'ftp://example.com/opowiadanie',
                'coordinator_notes': '', 'illustrated_excerpt': '', '_save': 'Zapisz',
                '_edit_version': page.context['request'].edit_version_token}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('story_url', response.context['adminform'].form.errors)
        response = self.client.post(url, {**data, 'story_url': 'https://example.com/opowiadanie'})
        self.assertEqual(response.status_code, 302)
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.story_url, 'https://example.com/opowiadanie')
