"""Widoki, których treść zależy od roli: pulpit samego recenzenta i przycisk dodawania zgłoszeń recenzji."""
import re

from django.test import TestCase
from django.urls import reverse

from fantazmaty.testing import create_member, create_superuser
from people.models import Role


def with_roles(username, *roles):
    user = create_member(username, roles[0])
    user.person_profile.roles.add(*(Role.objects.get_or_create(name=name)[0] for name in roles[1:]))
    return user


class ReviewerOnlyDashboardTests(TestCase):
    """Pulpit w trybie recenzenta (liczniki prowadzą do „Moich recenzji”) tylko dla osoby, której jedyną rolą jest
    Recenzent; inna rola, uprawnienia koordynatora albo superusera przywracają zwykły pulpit pracy nad tekstami."""

    @classmethod
    def setUpTestData(cls):
        admin_reviewer = create_member('dash-admin-reviewer', 'Recenzent')
        admin_reviewer.is_superuser = True
        admin_reviewer.save()
        cls.cases = [
            (create_member('dash-reviewer', 'Recenzent'), True),
            (with_roles('dash-reviewer-editor', 'Recenzent', 'Redaktor'), False),
            (with_roles('dash-coordinator', 'Recenzent', 'Koordynator redakcji'), False),
            (admin_reviewer, False),
            (create_member('dash-editor', 'Redaktor'), False),
            (create_superuser('dash-admin'), False),
        ]

    def test_reviewer_mode_only_for_member_whose_sole_role_is_reviewer(self):
        reviews_link = f'href="{reverse("core:my_reviews")}?view=active"'
        texts_link = f'href="{reverse("core:my_texts")}?view=active"'
        for user, reviewer_only in self.cases:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                page = self.client.get(reverse('core:home')).content.decode()
                self.assertEqual(reviews_link in page, reviewer_only)
                self.assertEqual(texts_link in page, not reviewer_only)
                self.assertEqual('Teksty, które recenzujesz.' in page, reviewer_only)


class ReviewImportButtonTests(TestCase):
    """Dodawanie zgłoszeń (dane osobowe autorów, ostrzeżenia z czarnej listy) – przycisk tylko dla superusera."""

    def test_only_superuser_sees_add_submissions_button_on_review_list(self):
        button = re.compile(rf'href="{reverse("core:review_bulk_import")}"\s+class="primary-button"')
        for user, visible in ((create_member('import-coordinator', 'Koordynator redakcji'), False),
                              (create_member('import-reviewer', 'Recenzent'), False),
                              (create_superuser('import-admin'), True)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                page = self.client.get(reverse('core:review_list'))
                self.assertEqual(page.status_code, 200)
                self.assertEqual(bool(button.search(page.content.decode())), visible)


class RoleNamesPageTests(TestCase):
    """Lista osób według roli (superuser): tylko osoby z wybraną rolą, komunikat dla roli bez osób."""

    def test_page_lists_people_with_selected_role_and_reports_empty_role(self):
        create_member('names-member', 'Redaktor')
        create_member('names-other', 'Korektor')
        empty = Role.objects.get_or_create(name='Grafik')[0]  # rolę tworzy też seed strony WWW (core 0060)
        self.client.force_login(create_superuser('names-admin'))
        url = reverse('core:role_names')
        page = self.client.get(url, {'role': Role.objects.get(name='Redaktor').pk})
        self.assertContains(page, '<p class="role-names">names-member Testowy</p>', html=True)
        self.assertContains(self.client.get(url, {'role': empty.pk}), 'Brak aktywnych osób z tą rolą.')
        self.assertNotContains(self.client.get(url, {'role': 'abc'}), 'class="role-names"')
