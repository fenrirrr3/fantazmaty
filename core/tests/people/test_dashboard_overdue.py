"""Pulpit koordynatora: belka „Zaległe etapy” nad zwykłymi sekcjami – tylko dla koordynatorów."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone
from lxml import html

from fantazmaty.testing import create_member, create_superuser
from people.models import Role
from texts.models import Anthology, Review, Text
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

    def test_pending_publication_section_only_for_superuser(self):
        """Sekcja „Przyjęte teksty do dodania…” i siatka zgłoszeń są tylko u superusera; koordynatorowi nic nie liczymy."""
        review = Review.objects.create(
            title='Czeka na proces', anthology=Anthology.objects.get(title='Cienie miasta'), author_first_name='Jan',
            author_last_name='Autor', email='autor@example.com', length=100, status=Review.Status.ACCEPTED,
            author_notified_at=timezone.localdate())
        for user, visible in ((self.superuser, True), (self.coordinator, False), (self.editor, False)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse('core:home'))
                page = html.fromstring(response.content)
                headings = page.xpath('//h2[text()="Przyjęte teksty do dodania do procesu wydawniczego"]')
                self.assertEqual(bool(headings), visible)
                self.assertEqual(bool(page.xpath('//*[contains(@class, "dashboard-intake-grid")]')), visible)
                pending = response.context['pending_publication_reviews']
                if visible:
                    self.assertEqual(list(pending), [review])
                else:
                    self.assertEqual(pending, [])

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

    def test_review_coordinator_scope_adds_reviews_without_stages(self):
        """Koordynator recenzji: zakres „recenzje” bez etapów; razem z redakcją – etapy redakcji i oba zakresy."""
        from core.views.dashboard import OVERDUE_SCOPES, overdue_scope
        editing = list(OVERDUE_SCOPES['Koordynator redakcji'][0])
        for roles, expected in ((['Koordynator recenzji'], ([], True, 'recenzje')),
                                (['Koordynator redakcji', 'Koordynator recenzji'],
                                 (editing, True, 'redakcja, recenzje'))):
            with self.subTest(roles=roles):
                user = create_member(f'pulpit-zakres-{len(roles)}', 'Koordynator ilustracji')
                user.person_profile.roles.set([Role.objects.get_or_create(name=role)[0] for role in roles])
                self.assertEqual(overdue_scope(user), expected)

    def test_no_banner_without_overdue_work(self):
        Stage.objects.filter(text__title__in=['Praca 40 dni', 'Czeka 12 dni']).delete()
        self.assertIsNone(self.banner(self.coordinator))


class OverdueBannerMatchesReportTests(TestCase):
    """Lekka ścieżka belki (inactivity_overview) daje te same pierwsze wiersze i tę samą liczbę
    co raport „Przestoje” (workflow_inactivity_context) – kryteria i kolejność nie mogą się rozjechać."""

    @classmethod
    def setUpTestData(cls):
        from fantazmaty.testing import create_member as member
        cls.superuser = create_superuser('przestoje-admin')
        cls.performer = member('przestoje-redaktor', 'Redaktor')
        today = timezone.localdate()
        book = Anthology.objects.create(title='Zegary')
        abandoned = Anthology.objects.create(title='Porzucona', status=Anthology.Status.ABANDONED)

        def stage(title, kind, *, anthology=book, queued=None, started=None, person=None, role='editor', **extra):
            text = Text.objects.filter(title=title).first() or Text.objects.create(
                title=title, length=1, anthology=anthology)
            assignment = (WorkflowRoleAssignment.objects.create(text=text, role=role, assigned_to=person)
                          if person else None)
            created = Stage.objects.create(text=text, stage_type=kind, started_at=started, assignment=assignment,
                                           **extra)
            Stage.objects.filter(pk=created.pk).update(queued_at=queued)
            return created

        days = lambda count: today - timedelta(days=count)  # noqa: E731
        stage('W toku 40 dni', 'editing', started=days(40), queued=days(41), person=cls.performer)
        stage('W toku 3 dni', 'editing', started=days(3), queued=days(3))
        stage('Rezerwacja', 'first_verification', queued=days(20), person=cls.performer, role='verifier_1')
        stage('Czeka bez daty', 'ready_for_editing', queued=None)
        # Zablokowana weryfikacja (otwarta redakcja) czeka „od dziś” – nie zalega.
        stage('Zablokowana', 'editing', started=days(2), queued=days(35))
        stage('Zablokowana', 'first_verification', queued=days(30))
        # Poprzednik zakończony później niż wejście do kolejki wyznacza początek oczekiwania.
        stage('Po kontroli', 'editing', started=days(50), ended_at=days(15), is_completed=True)
        stage('Po kontroli', 'editing_control', queued=days(30))
        stage('Wycofany', 'editing', started=days(50), queued=days(50))
        stage('Wycofany', 'withdrawn')
        stage('W porzuconej', 'editing', anthology=abandoned, started=days(60), queued=days(60))
        # Etap z poprzedniego cyklu nie zalega; liczy się bieżący cykl tekstu.
        stage('Inny cykl', 'editing', started=days(70), queued=days(70))
        Text.objects.filter(title='Inny cykl').update(current_workflow_cycle=2)
        stage('Inny cykl', 'first_proofreading', queued=days(9), workflow_cycle=2)
        # Te same liczby dni – kolejność po tytule bez względu na wielkość liter, potem po etapie.
        for index, title in enumerate(('ćma', 'Ala', 'ala', 'Żuraw', 'bez', 'Bez', 'ósmy', 'Ósmy', 'zet', 'Zet')):
            stage(title, 'first_proofreading' if index % 2 else 'ready_for_editing', queued=days(12))
        stage('Ala', 'second_proofreading', queued=days(12))

    @tag('slow')
    def test_first_rows_and_count_match_the_report(self):
        from django.http import QueryDict
        from core.selectors.reports import inactivity_overview, workflow_inactivity_context
        from core.views.dashboard import OVERDUE_SCOPES
        scopes = [[], *(stages for stages, _ in OVERDUE_SCOPES.values())]
        report = workflow_inactivity_context(user=self.superuser, params=QueryDict(''))['rows']
        titles = {row['text']['title'] for row in report}
        self.assertGreater(len(report), 10)
        self.assertTrue({'W toku 40 dni', 'Rezerwacja', 'Czeka bez daty', 'Po kontroli', 'Inny cykl'} <= titles)
        self.assertFalse({'W toku 3 dni', 'Zablokowana', 'Wycofany', 'W porzuconej'} & titles)
        for mode in ('all', 'active', 'waiting'):
            for stages in scopes:
                for active_days, waiting_days in ((28, 7), (2, 1), (45, 13)):
                    params = QueryDict(mutable=True)
                    params['mode'] = mode
                    params.setlist('stage', stages)
                    kwargs = dict(user=self.superuser, params=params, active_days=active_days,
                                  waiting_days=waiting_days)
                    rows = workflow_inactivity_context(**kwargs)['rows']
                    for limit in (3, 10):
                        with self.subTest(mode=mode, stages=stages, days=(active_days, waiting_days), limit=limit):
                            self.assertEqual(inactivity_overview(limit=limit, **kwargs), (rows[:limit], len(rows)))

    def test_dashboard_banner_uses_the_light_path(self):
        """Pulpit nie buduje całego raportu przestojów, tylko pokazywane wiersze i liczbę."""
        from core.views import dashboard
        self.client.force_login(self.superuser)
        with patch('core.selectors.reports.workflow_inactivity_context') as full:
            overdue = self.client.get(reverse('core:home')).context['overdue']
        full.assert_not_called()
        self.assertEqual(len(overdue['rows']) + len(overdue['extra_rows']), dashboard.OVERDUE_ON_DASHBOARD)
        self.assertEqual(overdue['more'], overdue['count'] - dashboard.OVERDUE_ON_DASHBOARD)
