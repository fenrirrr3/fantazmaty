"""Pulpit koordynatora: belka „Zaległe etapy” nad zwykłymi sekcjami – tylko dla koordynatorów."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from fantazmaty.testing import create_member, create_superuser
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

    def test_long_list_is_shortened_with_link_to_report(self):
        with patch('core.views.dashboard.OVERDUE_ON_DASHBOARD', 1):
            banner = self.banner(self.coordinator)
        self.assertEqual(len(banner.xpath('.//li[contains(@class, "overdue-item")]')), 1)
        self.assertIn('i jeszcze 1', banner.xpath('string(.//p[@class="overdue-footer"])'))

    def test_no_banner_without_overdue_work(self):
        Stage.objects.filter(text__title__in=['Praca 40 dni', 'Czeka 12 dni']).delete()
        self.assertIsNone(self.banner(self.coordinator))
