"""Anthology corrections: submission, idempotent retries, versions and resolved records."""
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

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
