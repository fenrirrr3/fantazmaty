"""Nawigacja panelu administracyjnego: grupy menu, skróty, kolejność sekcji formularzy i dostęp."""
import re

from django.contrib import admin
from django.contrib.admin.options import BaseModelAdmin
from django.templatetags.static import static
from django.test import RequestFactory, TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from authors.models import Author
from fantazmaty.testing import create_superuser
from people.models import Person
from texts.models import Anthology, Review, Text
from workflow.models import WorkflowRoleAssignment as A
from workflow.models import WorkflowStage as S


def marker_positions(test, content, markers):
    """Sprawdza, że wszystkie znaczniki są na stronie i występują w podanej kolejności."""
    test.assertEqual([marker for marker in markers if marker not in content], [])
    positions = [content.index(marker) for marker in markers]
    test.assertEqual(positions, sorted(positions))


class AdminNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_superuser()
        cls.book = Anthology.objects.create(title='Test')
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email='autor@example.com')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.text.authors.add(cls.author)
        Person.objects.create(user=cls.user, first_name='Jan', last_name='Administrator',
                              email=cls.user.email, is_active=True)

    def setUp(self):
        self.client.force_login(self.user)

    def app_list(self):
        request = RequestFactory().get(reverse('admin:index'))
        request.user = self.user
        return admin.site.get_app_list(request)

    def test_task_groups_order_and_details(self):
        groups = self.app_list()
        self.assertEqual(
            [g['app_label'] for g in groups],
            ['submissions', 'texts', 'publication', 'art', 'audio', 'people', 'promotion', 'recruitment',
             'settings', 'history'],
        )
        self.assertEqual([g['collapsed'] for g in groups],
                         [False, False, False, True, True, True, True, True, True, True])
        by_label = {group['app_label']: group for group in groups}

        def names(group):
            return [m['object_name'].lower() for m in group['models']]

        self.assertIn('anthologytask', names(by_label['publication']))
        self.assertIn('postlayoutassignment', names(by_label['publication']))
        self.assertNotIn('illustration', names(by_label['publication']))
        self.assertIn('texttranslation', names(by_label['texts']))
        self.assertIn('novelprofile', names(by_label['texts']))
        self.assertIn('vocabularyterm', names(by_label['texts']))
        # Ekstrakty zamykają grupę Teksty, tak jak w menu strony.
        self.assertEqual(names(by_label['texts'])[-2:], ['extract', 'extractvolume'])
        self.assertIn('author', names(by_label['people']))
        self.assertEqual(names(by_label['promotion']), ['textsocialmedia', 'socialmediaentry', 'socialmediaquote'])
        self.assertIn('workflowstage', names(by_label['history']))
        self.assertIn('workflowroleassignment', names(by_label['history']))
        detail = by_label['history']['subgroups'][0]
        self.assertEqual(detail['name'], 'Szczegółowe rekordy')
        self.assertTrue(detail['collapsed'])
        self.assertIn('textnote', names(detail))
        self.assertIn('authornote', names(detail))
        self.assertIn('reviewassignment', names(detail))

    def test_accounts_and_groups_are_not_listed_but_account_screen_stays(self):
        """Nawigacja nie ma „Kont użytkowników” ani „Grup uprawnień” (także w „Pozostałych rekordach”), role są w „Ludziach”,
        a ekran konta nadal się otwiera (wejście z ekranu osoby)."""
        entries = [(group['app_label'], item) for top in self.app_list()
                   for group in (top, *top.get('subgroups', ())) for item in group['models']]
        listed = {(item['object_name'].lower(), label) for label, item in entries}
        for name in ('user', 'group'):
            with self.subTest(model=name):
                self.assertEqual([label for model, label in listed if model == name], [])
        self.assertIn(('role', 'people'), listed)
        page = self.client.get(reverse('admin:index'))
        for label in ('Konta użytkowników', 'Grupy uprawnień', 'Konta i uprawnienia'):
            with self.subTest(label=label):
                self.assertNotContains(page, label)
        self.assertEqual(self.client.get(reverse('admin:auth_user_change', args=[self.user.pk])).status_code, 200)
        with self.assertRaises(NoReverseMatch):
            reverse('admin:auth_group_changelist')

    def test_shortcuts_resolve_and_admin_does_not_load_client_table_pagination(self):
        keys = {'audiobooks_queue', 'audiobooks_blacklist', 'audio_description_tasks',
                'illustrators_active', 'illustrators_inactive'}
        entries = [item for group in self.app_list() for item in group['models']
                   if item['object_name'] in keys]
        self.assertEqual(len(entries), len(keys))
        for entry in entries:
            page = self.client.get(entry['admin_url'])
            self.assertEqual(page.status_code, 200, entry)
            self.assertNotIn('e=1', page.request['QUERY_STRING'])
        page = self.client.get(reverse('admin:index'))
        self.assertNotContains(page, 'core/pagination.js')
        self.assertNotContains(page, 'class="cms-pagination"')
        form = self.client.get(reverse('admin:texts_text_change', args=[self.text.pk]))
        self.assertEqual(form.status_code, 200)
        self.assertNotContains(form, 'core/pagination.js')
        self.assertContains(form, 'id="id_audiobook_blacklisted"', count=1)

    def test_navigation_same_on_index_and_form(self):
        for url in (reverse('admin:index'), reverse('admin:texts_text_change', args=[self.text.pk])):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            for label in ('Zgłoszenia', 'Antologie i wydanie', 'Grafika', 'Historia i diagnostyka',
                          'Szczegółowe rekordy'):
                self.assertContains(page, label)
            self.assertContains(page, 'data-admin-group="settings"')
            self.assertContains(page, 'data-cms-admin-user="' + str(self.user.pk) + '"')
            for route in ('texts_text', 'texts_review', 'people_person', 'texts_anthology'):
                self.assertContains(page, reverse('admin:' + route + '_changelist'))
            self.assertContains(page, static('core/admin-navigation.js'))
        page = self.client.get(reverse('admin:index'))
        group_ids = re.findall(r'<details id="([^"]+)"', page.content.decode())
        self.assertEqual(len(group_ids), len(set(group_ids)))

    def test_text_fieldsets_and_inlines_display_once_in_order(self):
        assignment = A.objects.create(text=self.text, role='editor', assigned_to=self.user)
        S.objects.create(text=self.text, stage_type='editing', assignment=assignment,
                         started_at=timezone.localdate())
        content = self.client.get(reverse('admin:texts_text_change', args=[self.text.pk])).content.decode()
        marker_positions(self, content, [
            'id="id_title"', 'Status i zarządzanie', 'Workflow – wykonawcy etapów',
            'Notatki koordynatora', 'Notatki do tekstu', 'Dane techniczne i pochodzenie importu',
        ])
        for marker in ('id="id_title"', 'id="id_coordinator_note"', 'id="workflow_stages-group"'):
            self.assertEqual(content.count(marker), 1)

    def test_review_fields_then_votes_then_source_link(self):
        review = Review.objects.create(
            title='Zgłoszenie', anthology=self.book, author=self.author, author_first_name='Jan',
            author_last_name='Autor', email=self.author.email, length=100, genre='fantasy',
        )
        content = self.client.get(reverse('admin:texts_review_change', args=[review.pk])).content.decode()
        marker_positions(self, content, [
            'id="id_title"', 'Autor – wyłącznie superuser', 'Decyzja', 'Oceny recenzentów',
            'Ogólne uwagi do zgłoszenia', 'Proces wydawniczy', 'Kontrola zgłoszenia',
        ])
        self.assertEqual(content.count('id="id_copied_text"'), 1)

    def test_anthology_tasks_before_cover_and_print(self):
        content = self.client.get(reverse('admin:texts_anthology_change', args=[self.book.pk])).content.decode()
        marker_positions(self, content, [
            'id="id_title"', 'id="production_tasks-group"', 'id="id_cover_status"', 'id="id_print_status"',
        ])

    def test_custom_admin_site_leaves_django_base_method_unchanged(self):
        """Chroni przed łataniem BaseModelAdmin przez własny AdminSite i przed powrotem starego widoku zbiorczego."""
        from core.admin_site import CMSAdminSite

        self.assertIsInstance(admin.site, CMSAdminSite)
        self.assertEqual(BaseModelAdmin.formfield_for_foreignkey.__module__, 'django.contrib.admin.options')
        with self.assertRaises(NoReverseMatch):
            reverse('core:bulk_text_action')
