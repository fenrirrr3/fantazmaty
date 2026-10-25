"""Strony zespołu: lista i profile osób (byli członkowie, etykiety ról, układ tabel), uprawnienia osoby i etykiety wykonań."""
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.permissions import is_coordinator
from core.selectors.texts import _assignment_data, _stage_data, text_list_context
from core.views.search import _search_people
from fantazmaty.testing import create_member, create_superuser
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from people.models import Person, Role, Vacation
from texts.models import Anthology, Text
from workflow.labels import assignment_label
from workflow.models import WorkflowRoleAssignment, WorkflowStage


def has_class(name):
    return f'contains(concat(" ",normalize-space(@class)," ")," {name} ")'


class TeamPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = create_superuser()
        cls.member = User.objects.create_user('member', 'member@example.com', first_name='Jan', last_name='Aktywny')
        cls.member_profile = Person.objects.create(user=cls.member, first_name='Jan', last_name='Aktywny',
                                                   email=cls.member.email)
        cls.disabled = User.objects.create_user('hidden@example.com', 'hidden@example.com', is_active=False,
                                                first_name='Adam', last_name='Nieaktywny')
        cls.disabled_profile = Person.objects.create(user=cls.disabled, first_name='Adam', last_name='Nieaktywny',
                                                     email=cls.disabled.email,
                                                     dropbox_email='hidden-dropbox@example.com')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_inactive_account_is_absent_from_team_and_marked_in_search(self):
        response = self.client.get(reverse('core:people_list'))
        self.assertNotContains(response, self.disabled.email)
        self.assertNotContains(response, 'Nieaktywny')
        self.assertContains(response, self.member.email)
        # Wyszukiwarka obejmuje byłych członków – wyraźnie oznaczonych i bez danych kontaktowych.
        found = _search_people('Nieaktywny', self.admin)
        self.assertEqual([(row['pk'], row['member_state'], row['email']) for row in found],
                         [(self.disabled_profile.pk, 'Nieaktywny', '')])

    def test_inactive_contact_is_hidden_in_profile_for_member_and_superuser(self):
        for viewer in (self.member, self.admin):
            self.client.force_login(viewer)
            response = self.client.get(reverse('core:person_detail', args=[self.disabled_profile.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, self.disabled.email)
            self.assertNotContains(response, self.disabled_profile.dropbox_email)
        response = self.client.get(reverse('core:person_permissions', args=[self.disabled_profile.pk]))
        self.assertNotContains(response, self.disabled.email)
        response = self.client.get(reverse('admin:people_person_change', args=[self.disabled_profile.pk]))
        self.assertContains(response, self.disabled.email)
        self.assertContains(response, self.disabled_profile.dropbox_email)

    def test_role_badges_do_not_invent_generic_coordinator_and_access_is_preserved(self):
        """Chroni przed wyświetlaniem wycofanej roli „Koordynator” zamiast faktycznych ról."""
        for index, roles in enumerate((['Koordynator redakcji', 'Prawa ręka'], [])):
            user = get_user_model().objects.create_user(f'labels-{index}')
            person = Person.objects.create(user=user, first_name='Jan', last_name=f'Osoba {index}',
                                           legacy_coordinator_access=not roles)
            person.roles.add(*(Role.objects.get_or_create(name=name)[0] for name in roles))
            Vacation.objects.create(person=person, start_date=timezone.localdate(), until_revoked=True)
            self.assertTrue(is_coordinator(user))
            for route, args in [('people_list', []), ('person_detail', [person.pk]), ('active_vacations', [])]:
                page = self.client.get(reverse(f'core:{route}', args=args))
                self.assertEqual(page.status_code, 200)
                doc = html.fromstring(page.content)
                labels = [node.text_content().strip() for node in doc.xpath(f'//*[{has_class("role-badge")}]')]
                self.assertNotIn('Koordynator', labels)
                self.assertNotIn('Koordynator zespołu', doc.text_content())
                if roles:
                    self.assertIn('Koordynator redakcji', labels)
                    self.assertIn('Prawa ręka', labels)
                else:
                    self.assertContains(page, 'Brak przypisanych ról')
            self.assertTrue(is_coordinator(user))
        self.assertFalse(Role.objects.filter(name='Koordynator').exists())

    def test_profile_tables_have_shared_inset_for_table_and_pagination(self):
        author = Author.objects.create(first_name='Anna', last_name='Test', email='author57@example.test')
        Text.objects.create(title='Opowiadanie', anthology=Anthology.objects.create(title='Antologia'),
                            length=100).authors.add(author)
        Person.objects.create(user=self.admin, first_name='Jan', last_name='Test')
        for route, pk, headings in [
            ('core:author_detail', author.pk, ['author-texts-heading', 'author-archive-heading']),
            ('core:person_detail', self.admin.person_profile.pk, ['person-assignments-heading']),
        ]:
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[pk]))
                self.assertEqual(response.status_code, 200)
                doc = html.fromstring(response.content)
                for heading in headings:
                    section = doc.xpath(f'//section[@aria-labelledby="{heading}"]')[0]
                    self.assertTrue(section.xpath(f'./div[{has_class("profile-table-inset")}]//table'))
                    self.assertFalse(section.xpath('./div[@class="table-container"]'))
                    self.assertFalse(section.xpath('./nav[contains(@class,"pagination")]'))


class PersonPermissionsTests(WorkflowTestDataMixin, TestCase):
    def permissions(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('core:person_permissions', args=[self.editor.person_profile.pk]))
        self.assertEqual(response.status_code, 200)
        return {row['label']: row['allowed'] for row in response.context['permissions']}

    def test_permissions_follow_roles_and_leave_blocks_all_new_claims(self):
        permissions = self.permissions()
        labels = dict(WorkflowRoleAssignment.Role.choices)
        self.assertTrue(permissions[labels[WorkflowRoleAssignment.Role.EDITOR]])
        for role in (WorkflowRoleAssignment.Role.VERIFIER_1, WorkflowRoleAssignment.Role.PROOFREADER_2,
                     WorkflowRoleAssignment.Role.STYLING):
            self.assertFalse(permissions[labels[role]])
        Vacation.objects.create(person=self.editor.person_profile, start_date=self.today, until_revoked=True)
        self.assertFalse(any(self.permissions().values()))


class WorkListLabelsTests(TestCase):
    def test_hide_ready_hides_withdrawn_and_ready_but_not_other_texts(self):
        admin = create_superuser()
        rows = {}
        for kind in ('ready', 'withdrawn', 'ready_for_editing'):
            text = Text.objects.create(title=kind, length=100)
            WorkflowStage.objects.create(text=text, stage_type=kind)
            rows[kind] = text.pk
        no_stage = Text.objects.create(title='Bez etapu', length=100)

        def ids(params):
            context = text_list_context(user=admin, params=QueryDict(params))
            return set(context['filtered_queryset'].values_list('pk', flat=True))

        self.assertEqual(ids('hide_ready=1'), {rows['ready_for_editing'], no_stage.pk})
        self.assertEqual(ids('hide_ready=0'), set(rows.values()) | {no_stage.pk})
        self.assertEqual(ids('hide_ready=1&status=withdrawn'), set())
        self.assertEqual(ids('hide_ready=1&status=ready'), set())
        self.assertEqual(ids('hide_ready=0&status=withdrawn'), {rows['withdrawn']})

    def test_short_execution_labels(self):
        for role, label, stage_kind in (
            ('editor', 'Redakcja', 'editing'),
            ('proofreader_1', 'Pierwsza korekta', 'first_proofreading'),
            ('verifier_2', 'Druga weryfikacja', 'second_verification'),
        ):
            assignment = WorkflowRoleAssignment(role=role, execution_number=2)
            self.assertEqual(_assignment_data(assignment)['get_role_display'],
                             assignment.get_role_display() + ' (wyk. 2)')
            self.assertEqual(_stage_data(WorkflowStage(stage_type=stage_kind, execution_number=2))[
                'get_stage_type_display'], label + ' (wyk. 2)')
            assignment.execution_number = 1
            self.assertEqual(assignment_label(assignment), assignment.get_role_display())


class PeopleListFilterTests(TestCase):
    """Lista zespołu: filtr ról i frazy; błędny filtr daje pustą listę z błędem zamiast całego zespołu."""

    @classmethod
    def setUpTestData(cls):
        cls.editor = create_member('lista-redaktor', 'Redaktor')
        cls.proofreader = create_member('lista-korektor', 'Korektor')
        cls.both = create_member('lista-oboje', 'Redaktor')
        cls.both.person_profile.roles.add(Role.objects.get(name='Korektor'))
        cls.editor_role = Role.objects.get(name='Redaktor')
        cls.proofreader_role = Role.objects.get(name='Korektor')

    def setUp(self):
        self.client.force_login(self.editor)

    def names(self, params, status=200):
        response = self.client.get(reverse('core:people_list'), params)
        self.assertEqual(response.status_code, status)
        return response, sorted(person.first_name for person in response.context['people'])

    def test_role_filters(self):
        """Filtr ról: osoba z kilkoma rolami pojawia się raz; fraza zawęża listę."""
        everyone = ['lista-korektor', 'lista-oboje', 'lista-redaktor']
        cases = {
            'fraza': ({'query': 'lista-korektor'}, ['lista-korektor']),
            'kilka ról': ({'roles': [self.editor_role.pk, self.proofreader_role.pk]}, everyone),
        }
        for name, (params, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.names(params)[1], expected)

    def test_invalid_filter_returns_no_people_instead_of_everyone(self):
        for params in ({'roles': ['999999']}, {'query': 'x' * 300}):
            with self.subTest(params=list(params)):
                response, names = self.names(params, status=400)
                self.assertEqual(names, [])
                self.assertTrue(response.context['filter_form'].errors)
