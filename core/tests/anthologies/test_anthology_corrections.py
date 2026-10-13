"""Uwagi do wydanych antologii: zgłaszanie (także przez AJAX), edycja i usuwanie przez autora uwagi, zmiana statusu,
wyszukiwanie oraz zmiany antologii lub opowiadania w trakcie zapisu."""
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.correction_forms import CorrectionForm
from core.models import AnthologyCorrection
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
        invalid = self.client.post(edit, {'anthology': self.book.pk, 'fragment': 'x', 'problem': '', 'suggestion': 'z',
                                          'version': item.updated_at.isoformat()})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('problem', invalid.context['form'].errors)
        delete = reverse('core:correction_delete', args=[item.pk])
        self.assertEqual(self.client.post(delete, {'version': 'old'}).status_code, 409)
        self.assertTrue(AnthologyCorrection.objects.exists())
        deleted = self.client.post(delete, {'version': item.updated_at.isoformat()})
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
        missing = self.client.post(url, {'selected': [first.pk, second.pk + 100], 'status': 'new'})
        self.assertEqual(missing.status_code, 409)
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

    def test_form_is_collapsed_above_list_and_opens_on_errors(self):
        """Sekcja „Zgłoś uwagę” stoi przed listą uwag, domyślnie zwinięta; po błędnym POST jest rozwinięta."""
        self.client.force_login(self.member)
        url = reverse('core:anthology_corrections')
        for case, response, opened in (('GET', self.client.get(url), False),
                                       ('POST z błędem', self.client.post(url, {'anthology': self.book.pk}), True)):
            with self.subTest(case):
                doc = html.fromstring(response.content)
                [section] = doc.xpath('//details[@id="zglos-uwage"]')
                self.assertEqual(section.xpath('string(summary/span[1])'), 'Zgłoś uwagę')
                self.assertEqual('open' in section.attrib, opened)
                self.assertTrue(section.xpath('.//form[@data-correction-form]'))
                table = doc.xpath('//table[@data-copy-table]')[0]
                self.assertEqual(section.xpath('following::table[@data-copy-table]'), [table])
        self.assertEqual(doc.xpath('//a[text()="Zgłoś pierwszą uwagę"]/@href'), ['#zglos-uwage'])


    def test_ajax_submission_answers_with_json(self):
        self.client.force_login(self.member)
        url = reverse('core:anthology_corrections')
        ok = self.client.post(url, {'anthology': self.book.pk, 'text': self.text.pk, 'fragment': 'a', 'problem': 'b',
                                    'suggestion': 'c', 'submission_token': str(uuid.uuid4())},
                              HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertEqual(body['id'], AnthologyCorrection.objects.get().pk)
        self.assertEqual(body['list_url'], url)
        uuid.UUID(body['next_token'])
        invalid = self.client.post(url, {'anthology': self.book.pk, 'fragment': '', 'problem': 'b', 'suggestion': 'c'},
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('fragment', invalid.json()['errors'])
        self.assertEqual(AnthologyCorrection.objects.count(), 1)

    def test_list_search_matches_title_and_correction_fields(self):
        for title, problem in (('Opowiadanie', 'Literówka w nazwisku'), ('Inne miejsce', 'Brak przecinka')):
            AnthologyCorrection.objects.create(anthology=self.book, story_title=title, fragment='f', problem=problem,
                                               suggestion='s', submitted_by=self.member)
        self.client.force_login(self.member)
        for query, expected in (('literówka', ['Opowiadanie']), ('inne MIEJSCE', ['Inne miejsce']), ('brak', ['Inne miejsce']),
                                ('nic takiego', [])):
            with self.subTest(query=query):
                page = self.client.get(reverse('core:anthology_corrections'), {'q': query})
                self.assertEqual([item.story_title for item in page.context['corrections']], expected)

    def test_anthology_or_story_changed_while_saving_is_rejected(self):
        """Zmiana statusu antologii albo przeniesienie opowiadania między walidacją a zapisem nie przechodzi."""
        other_ready = Anthology.objects.create(title='Druga gotowa', status=Anthology.Status.READY)
        item = self.correction()
        original_clean = CorrectionForm.clean

        def changing(change):
            def clean(form):
                data = original_clean(form)
                change()
                return data
            return clean

        def unready(book):
            return lambda: Anthology.objects.filter(pk=book.pk).update(status=Anthology.Status.IN_PREPARATION)

        def move_text():
            Text.objects.filter(pk=self.text.pk).update(anthology=self.draft)

        cases = [
            ('nowa – antologia już niegotowa', 'core:anthology_corrections', {'anthology': self.book.pk, 'text': ''},
             unready(self.book), 'anthology'),
            ('nowa – opowiadanie przeniesione', 'core:anthology_corrections', {}, move_text, 'text'),
            ('edycja – nowa antologia już niegotowa', 'core:correction_edit',
             {'anthology': other_ready.pk, 'text': ''}, unready(other_ready), 'anthology'),
            ('edycja – opowiadanie przeniesione', 'core:correction_edit', {}, move_text, 'text'),
        ]
        self.client.force_login(self.member)
        for label, name, values, change, field in cases:
            with self.subTest(label):
                Anthology.objects.filter(pk__in=[self.book.pk, other_ready.pk]).update(status=Anthology.Status.READY)
                Text.objects.filter(pk=self.text.pk).update(anthology=self.book)
                item.refresh_from_db()
                editing = name == 'core:correction_edit'
                url = reverse(name, args=[item.pk]) if editing else reverse(name)
                data = {'anthology': self.book.pk, 'text': self.text.pk, 'fragment': 'nowy', 'problem': 'p',
                        'suggestion': 's', **values}
                if editing:
                    data['version'] = item.updated_at.isoformat()
                with patch.object(CorrectionForm, 'clean', changing(change)):
                    response = self.client.post(url, data)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.context['form'].errors)
        self.assertEqual(list(AnthologyCorrection.objects.values_list('pk', 'fragment')), [(item.pk, 'a')])
