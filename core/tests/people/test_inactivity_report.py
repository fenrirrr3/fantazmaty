"""Raport nieaktywności: wiersze i tryby (praca, oczekiwanie), filtry etapów, sortowanie i wyszukiwanie z ochroną danych autora."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, Text
from workflow.models import WorkflowStage as Stage

URL = reverse('core:workflow_inactivity')


class InactivityReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = create_superuser('audit-admin')
        cls.coordinator = create_member('audit-coord', 'Koordynator recenzji')
        cls.book = Anthology.objects.create(title='Test audytu')

    def test_report_renders_populated_rows_and_unknown_dates(self):
        today = timezone.localdate()
        expected = {}
        for title, kind, started, queued in (
            ('Praca 40 dni', 'editing', today - timedelta(days=40), today),
            ('Czeka 12 dni', 'ready_for_editing', None, today - timedelta(days=12)),
            ('Nieznany czas', 'ready_for_editing', None, None),
            ('Krótka praca', 'editing', today - timedelta(days=3), today),
            ('Przyszła praca', 'editing', today + timedelta(days=3), today),
        ):
            text = Text.objects.create(title=title, length=1, anthology=self.book)
            stage = Stage.objects.create(text=text, stage_type=kind, started_at=started)
            Stage.objects.filter(pk=stage.pk).update(queued_at=queued)
            if title in ('Praca 40 dni', 'Czeka 12 dni', 'Nieznany czas'):
                expected[title] = stage.pk
        self.client.force_login(self.superuser)
        for params, titles in (
            ({}, ['Praca 40 dni', 'Czeka 12 dni', 'Nieznany czas']),
            ({'mode': 'active'}, ['Praca 40 dni']),
            ({'mode': 'waiting'}, ['Czeka 12 dni', 'Nieznany czas']),
            ({'stage': ['editing']}, ['Praca 40 dni']),
            ({'stage': ['editing', 'ready_for_editing'], 'sort': '-days'},
             ['Praca 40 dni', 'Czeka 12 dni', 'Nieznany czas']),
        ):
            with self.subTest(params=params):
                response = self.client.get(URL, params)
                self.assertEqual(response.status_code, 200)
                rows = list(response.context['rows'])
                self.assertEqual([row['text']['title'] for row in rows], titles)
                self.assertEqual([row['stage']['pk'] for row in rows], [expected[title] for title in titles])
        response = self.client.get(URL)
        self.assertContains(response, 'Nieznany czas')
        self.assertNotContains(response, 'Przyszła praca')
        self.assertNotContains(response, 'Krótka praca')

    def test_search_is_polish_and_preserves_author_permissions(self):
        author = Author.objects.create(first_name='Łucja', last_name='Żółć', email='private-author@example.test')
        text = Text.objects.create(title='Zażółć gęślą', length=1, anthology=self.book)
        text.authors.add(author)
        Stage.objects.create(text=text, stage_type='editing', started_at=timezone.localdate() - timedelta(days=40))
        self.client.force_login(self.superuser)
        for query in ('zazolc gesla', 'LUCJA ZOLC', 'Test audytu redakcja'):
            with self.subTest(query=query):
                response = self.client.get(URL, {'q': query})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.context['rows']), 1)
                self.assertContains(response, text.title)
        self.client.force_login(self.coordinator)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, text.title)
        self.assertNotContains(response, str(author))
        self.assertNotContains(response, author.email)
        self.assertEqual(len(self.client.get(URL, {'q': 'lucja zolc'}).context['rows']), 0)
