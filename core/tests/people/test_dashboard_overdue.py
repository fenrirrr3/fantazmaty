"""Pulpit koordynatora: belka „Zaległe etapy” nad zwykłymi sekcjami – tylko dla koordynatorów."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from fantazmaty.testing import create_member, create_superuser
from people.models import Role
from texts.models import Anthology, Text
from workflow.models import WorkflowRoleAssignment, WorkflowStage as Stage


class DashboardOverdueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.coordinator = create_member('pulpit-koord', 'Koordynator')
        cls.editor = create_member('pulpit-redaktor', 'Redaktor')
        cls.superuser = create_superuser('pulpit-admin')
        book = Anthology.objects.create(title='Cienie miasta')
        today = timezone.localdate()
        cls.rows = {}
        for title, kind, started, queued, person in (
            ('Praca 40 dni', 'editing', today - timedelta(days=40), today, cls.editor),
            ('Czeka 12 dni', 'ready_for_editing', None, today - timedelta(days=12), None),
            ('Krótka praca', 'editing', today - timedelta(days=3), today, None),
        ):
            text = Text.objects.create(title=title, length=1, anthology=book)
            assignment = (WorkflowRoleAssignment.objects.create(text=text, role='editor', assigned_to=person)
                          if person else None)
            stage = Stage.objects.create(text=text, stage_type=kind, started_at=started, assignment=assignment)
            Stage.objects.filter(pk=stage.pk).update(queued_at=queued)
            cls.rows[title] = text

    def banner(self, user):
        self.client.force_login(user)
        page = html.fromstring(self.client.get(reverse('core:home')).content)
        found = page.xpath('//*[@data-overdue-banner]')
        return found[0] if found else None

    def test_only_coordinators_see_the_banner(self):
        for user, visible in ((self.coordinator, True), (self.superuser, True), (self.editor, False)):
            with self.subTest(user=user.username):
                self.assertEqual(self.banner(user) is not None, visible)

    def test_banner_lists_overdue_stages_longest_first(self):
        banner = self.banner(self.coordinator)
        items = banner.xpath('.//li[contains(@class, "overdue-item")]')
        self.assertEqual([item.xpath('string(.//a[@class="overdue-title"])') for item in items],
                         ['Praca 40 dni', 'Czeka 12 dni'])
        first, second = items
        self.assertEqual(first.xpath('string(.//span[@class="overdue-days"]/strong)'), '40')
        self.assertIn('is-active', first.get('class'))
        self.assertEqual(first.xpath('string(.//span[@class="overdue-meta"])'),
                         f'Cienie miasta · {self.editor.person_profile}')
        self.assertIn('czeka od', second.xpath('string(.//span[@class="overdue-kind"])'))
        self.assertEqual(first.xpath('string(.//a[@class="overdue-title"]/@href)'),
                         reverse('core:assigned_text_detail', args=[self.rows['Praca 40 dni'].pk]))
        self.assertEqual(banner.xpath('string(.//span[@class="overdue-count"])'), '2')

    def test_long_list_shows_first_rows_then_expands_up_to_limit(self):
        """Widoczne od razu są pierwsze pozycje, kolejne do limitu – po rozwinięciu, nadmiar – tylko link do raportu."""
        for visible, limit, shown, hidden, more in ((1, 2, 1, 1, ''), (1, 1, 1, 0, 'i jeszcze 1'),
                                                    (3, 10, 2, 0, '')):
            with self.subTest(visible=visible, limit=limit), \
                    patch('core.views.dashboard.OVERDUE_VISIBLE', visible), \
                    patch('core.views.dashboard.OVERDUE_ON_DASHBOARD', limit):
                banner = self.banner(self.coordinator)
                self.assertEqual(len(banner.xpath('./ol/li[contains(@class, "overdue-item")]')), shown)
                extra = banner.xpath('./details[@class="overdue-more"]')
                self.assertEqual(len(extra[0].xpath('.//li[contains(@class, "overdue-item")]')) if extra else 0, hidden)
                if extra:
                    self.assertEqual(extra[0].xpath('string(./summary/span[@class="overdue-more-show"])'),
                                     f'Pokaż kolejne {hidden}')
                    self.assertEqual(extra[0].xpath('string(./ol/@start)'), str(shown + 1))
                footer = banner.xpath('string(.//p[@class="overdue-footer"])')
                if more:
                    self.assertIn(more, footer)
                else:
                    self.assertNotIn('i jeszcze', footer)

    def test_dashboard_limits_are_three_visible_and_ten_in_total(self):
        from core.views import dashboard
        self.assertEqual((dashboard.OVERDUE_VISIBLE, dashboard.OVERDUE_ON_DASHBOARD), (3, 10))

    def test_banner_is_limited_to_the_coordinator_department(self):
        """Koordynator działu widzi tylko zaległości swojego działu (redakcja/weryfikacja/korekta/recenzje);
        koordynator bez działu i superuser – wszystko; koordynator innego działu – nic."""
        book = Anthology.objects.get(title='Cienie miasta')
        today = timezone.localdate()
        for title, kind in (('Weryfikacja 30 dni', 'first_verification'), ('Korekta 30 dni', 'first_proofreading')):
            text = Text.objects.create(title=title, length=1, anthology=book)
            stage = Stage.objects.create(text=text, stage_type=kind, started_at=today - timedelta(days=30))
            Stage.objects.filter(pk=stage.pk).update(queued_at=today)
        cases = (
            ('Koordynator redakcji', ['Praca 40 dni', 'Czeka 12 dni'], 'redakcja'),
            ('Koordynator weryfikacji', ['Weryfikacja 30 dni'], 'weryfikacja'),
            ('Koordynator korekty', ['Korekta 30 dni'], 'korekta'),
            ('Koordynator', ['Praca 40 dni', 'Korekta 30 dni', 'Weryfikacja 30 dni', 'Czeka 12 dni'], ''),
            ('Koordynator ilustracji', [], ''),
        )
        for role, titles, scope in cases:
            with self.subTest(role=role):
                user = create_member(f'pulpit-{role.casefold().replace(" ", "-")}', 'Koordynator ilustracji')
                user.person_profile.roles.set([Role.objects.get_or_create(name=role)[0]])  # bez aliasu z create_member
                banner = self.banner(user)
                if not titles:
                    self.assertIsNone(banner)
                    continue
                items = banner.xpath('.//li[contains(@class, "overdue-item")]')
                self.assertEqual([item.xpath('string(.//a[@class="overdue-title"])') for item in items], titles)
                header = banner.xpath('string(.//header/div/p)')
                self.assertEqual('Zakres: ' in header, bool(scope))
                self.assertIn(scope, header)
                link = banner.xpath('string(.//a[@class="secondary-button"]/@href)')
                self.assertEqual('stage=' in link, bool(scope))

    def test_no_banner_without_overdue_work(self):
        Stage.objects.filter(text__title__in=['Praca 40 dni', 'Czeka 12 dni']).delete()
        self.assertIsNone(self.banner(self.coordinator))
