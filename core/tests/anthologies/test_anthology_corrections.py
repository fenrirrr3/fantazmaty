"""Uwagi do wydanych antologii: zgłaszanie, edycja i usuwanie przez autora uwagi, zmiana statusu oraz import z pliku."""
import json
import uuid
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.models import AnthologyCorrection
from fantazmaty.testing import create_member
from people.models import Person
from texts.models import Anthology, Text


class CorrectionViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('admin', 'admin@example.test', 'x')
        cls.member = User.objects.create_user('member', 'member@example.test', 'x')
        Person.objects.create(user=cls.member, first_name='Jan', last_name='Test', email=cls.member.email)
        cls.other = User.objects.create_user('other', 'other@example.test', 'x')
        Person.objects.create(user=cls.other, first_name='Anna', last_name='Test', email=cls.other.email)
        cls.book = Anthology.objects.create(title='Gotowa', status=Anthology.Status.READY)
        cls.draft = Anthology.objects.create(title='W przygotowaniu')
        cls.text = Text.objects.create(title='Opowiadanie', length=100, anthology=cls.book)

    def submit(self, client=None, **extra):
        data = {'anthology': self.book.pk, 'text': self.text.pk, 'fragment': 'Ala ma kota',
                'problem': 'Literówka', 'suggestion': 'Ala ma psa', **extra}
        return (client or self.client).post(reverse('core:anthology_corrections'), data)

    def test_member_submits_correction_with_story_title(self):
        self.client.force_login(self.member)
        self.assertEqual(self.submit().status_code, 302)
        item = AnthologyCorrection.objects.get()
        self.assertEqual(item.story_title, 'Opowiadanie')
        self.assertEqual(item.submitted_by, self.member)

    def test_retry_with_same_token_does_not_duplicate(self):
        """Błąd: ponowne wysłanie formularza (np. po zerwanym połączeniu) dublowało uwagę."""
        self.client.force_login(self.member)
        token = str(uuid.uuid4())
        for _ in range(2):
            self.submit(submission_token=token)
        self.assertEqual(AnthologyCorrection.objects.count(), 1)
        conflict = self.client.post(reverse('core:anthology_corrections'), {
            'anthology': self.book.pk, 'fragment': 'inny', 'problem': 'inny', 'suggestion': 'inny',
            'submission_token': token}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(conflict.status_code, 409)

    def test_only_ready_anthologies_are_accepted(self):
        self.client.force_login(self.member)
        response = self.submit(anthology=self.draft.pk, text='')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AnthologyCorrection.objects.exists())

    def test_texts_endpoint_lists_only_ready_anthology(self):
        self.client.force_login(self.member)
        url = reverse('core:correction_texts')
        self.assertEqual(self.client.get(url, {'anthology': self.book.pk}).json()['texts'], [{'id': self.text.pk, 'title': 'Opowiadanie'}])
        self.assertEqual(self.client.get(url, {'anthology': self.draft.pk}).json()['texts'], [])
        self.assertEqual(self.client.get(url, {'anthology': 'x'}).json()['texts'], [])

    def correction(self, **fields):
        return AnthologyCorrection.objects.create(anthology=self.book, text=self.text, story_title='Opowiadanie',
                                                  fragment='a', problem='b', suggestion='c', submitted_by=self.member, **fields)

    def test_edit_and_delete_belong_to_submitter_and_need_current_version(self):
        item = self.correction()
        edit = reverse('core:correction_edit', args=[item.pk])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(edit).status_code, 404)
        self.client.force_login(self.member)
        stale = self.client.post(edit, {'anthology': self.book.pk, 'fragment': 'x', 'problem': 'y', 'suggestion': 'z', 'version': 'old'})
        self.assertEqual(stale.status_code, 409)
        version = item.updated_at.isoformat()
        saved = self.client.post(edit, {'anthology': self.book.pk, 'text': self.text.pk, 'fragment': 'x', 'problem': 'y', 'suggestion': 'z', 'version': version})
        self.assertEqual(saved.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.fragment, 'x')
        deleted = self.client.post(reverse('core:correction_delete', args=[item.pk]), {'version': item.updated_at.isoformat()})
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(AnthologyCorrection.objects.exists())

    def test_resolved_correction_is_read_only(self):
        item = self.correction(status=AnthologyCorrection.Status.APPLIED)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('core:correction_edit', args=[item.pk])).status_code, 403)
        response = self.client.post(reverse('core:correction_delete', args=[item.pk]), {'version': item.updated_at.isoformat()})
        self.assertEqual(response.status_code, 403)

    def test_superuser_changes_status_only_with_current_versions(self):
        first, second = self.correction(), self.correction()
        url = reverse('core:correction_status')
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url, {'selected': [first.pk], 'status': 'accepted'}).status_code, 403)
        self.client.force_login(self.admin)
        stale = self.client.post(url, {'selected': [first.pk, second.pk], 'status': 'accepted',
                                       f'version_{first.pk}': first.updated_at.isoformat(), f'version_{second.pk}': 'old'})
        self.assertEqual(stale.status_code, 409)
        first.refresh_from_db()
        self.assertEqual(first.status, 'new')
        ok = self.client.post(url, {'selected': [first.pk, second.pk], 'status': 'applied',
                                    f'version_{first.pk}': first.updated_at.isoformat(), f'version_{second.pk}': second.updated_at.isoformat()})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(set(AnthologyCorrection.objects.values_list('status', flat=True)), {'applied'})
        again = self.client.post(url, {'selected': [first.pk], 'status': 'new', f'version_{first.pk}': 'x'})
        self.assertEqual(again.status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.status, 'applied')

    def test_invalid_status_request_is_rejected_without_changes(self):
        item = self.correction()
        self.client.force_login(self.admin)
        response = self.client.post(reverse('core:correction_status'), {'selected': ['abc'], 'status': 'accepted'})
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.status, 'new')

    def test_published_anthology_notice_is_under_the_heading(self):
        self.client.force_login(self.admin)
        doc = html.fromstring(self.client.get(reverse("core:anthology_corrections")).content)
        self.assertIn("Tu zgłaszamy uwagi tylko do już wydanych antologii",
                      doc.xpath('string(//header[h1="Uwagi do antologii"]/p)'))


class CorrectionImportTests(TestCase):
    def setUp(self):
        self.reporter = create_member('import-reporter', 'Redaktor')
        self.reporter.first_name, self.reporter.last_name = 'Anna', 'Zgłaszająca'
        self.reporter.save()
        person = self.reporter.person_profile
        person.first_name, person.last_name = 'Anna', 'Zgłaszająca'
        person.save()
        self.book = Anthology.objects.create(title='Antologia wydana', status=Anthology.Status.READY)
        self.text = Text.objects.create(title='Opowiadanie pierwsze', anthology=self.book, length=10)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def write(self, rows, accounts=None):
        path = Path(self.temp.name) / 'uwagi.json'
        payload = {'schema': 'anthology-corrections-v1', 'corrections': rows}
        if accounts is not None:
            payload['accounts'] = accounts
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return str(path)

    def run_import(self, path, *extra):
        out = StringIO()
        call_command('import_anthology_corrections', path, *extra, stdout=out)
        return json.loads(out.getvalue())

    def rows(self):
        return [
            {'anthology': 'Antologia wydana', 'reporter': 'Anna Zgłaszająca', 'story': 'Opowiadanie pierwsze',
             'fragment': 'Ala ma kota', 'problem': 'literówka', 'suggestion': 'Ala ma kota.'},
            {'anthology': 'antologia  WYDANA', 'reporter': 'Karolina', 'story': 'Inne miejsce',
             'fragment': 'Paginacja', 'problem': 'zła numeracja', 'suggestion': ''},
            {'anthology': 'Antologia wydana', 'reporter': 'Karolina', 'story': 'Audiodeskrypcja',
             'fragment': 'tytuł w cudzysłowie', 'problem': '', 'suggestion': ''},
        ]

    def test_preview_then_apply_then_repeat(self):
        path = self.write(self.rows())
        self.assertEqual(self.run_import(path)['created'], 3)
        self.assertFalse(AnthologyCorrection.objects.exists())
        report = self.run_import(path, '--apply')
        self.assertEqual(report['created'], 3)
        first = AnthologyCorrection.objects.get(fragment='Ala ma kota')
        self.assertEqual((first.text, first.submitted_by, first.story_title), (self.text, self.reporter, 'Opowiadanie pierwsze'))
        other = AnthologyCorrection.objects.get(fragment='Paginacja')
        self.assertIsNone(other.submitted_by)
        self.assertEqual((other.reporter_name, other.story_title, other.text), ('Karolina', 'Inne miejsce', None))
        self.assertEqual(AnthologyCorrection.objects.get(fragment='tytuł w cudzysłowie').story_title, 'Audiodeskrypcja')
        again = self.run_import(path, '--apply')
        self.assertEqual((again['created'], again['unchanged']), (0, 3))
        self.client.force_login(self.reporter)
        page = self.client.get(reverse('core:anthology_corrections'))
        self.assertContains(page, 'Karolina')

    def test_inactive_accounts_are_created_for_reporters(self):
        accounts = [{'first_name': 'Karolina', 'last_name': 'Nowa', 'email': 'karolina.nowa.ab12@fantazmaty.invalid'},
                    {'first_name': 'Anna', 'last_name': 'Zgłaszająca', 'email': 'anna.duplikat@fantazmaty.invalid'}]
        rows = self.rows()
        for row in rows[1:]:
            row['reporter'] = 'Karolina Nowa'
        path = self.write(rows, accounts)
        preview = self.run_import(path)
        self.assertEqual(len(preview['accounts_created']), 1)
        self.assertFalse(get_user_model().objects.filter(last_name='Nowa').exists())
        self.run_import(path, '--apply')
        user = get_user_model().objects.get(last_name='Nowa')
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(AnthologyCorrection.objects.filter(submitted_by=user).count(), 2)
        self.assertFalse(get_user_model().objects.filter(email='anna.duplikat@fantazmaty.invalid').exists())
        again = self.run_import(path, '--apply')
        self.assertEqual((again['accounts_created'], again['created']), ([], 0))

    def test_unknown_or_unpublished_anthology_stops_everything(self):
        Anthology.objects.create(title='W przygotowaniu')
        for title in ('Nie ma takiej', 'W przygotowaniu'):
            rows = self.rows()
            rows[1]['anthology'] = title
            with self.assertRaises(CommandError):
                self.run_import(self.write(rows), '--apply')
        self.assertFalse(AnthologyCorrection.objects.exists())
