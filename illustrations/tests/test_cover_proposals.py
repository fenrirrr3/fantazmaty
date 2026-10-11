"""Propozycje ilustracji okładkowych: zgłaszanie przez zespół (pola kontrolowane przez serwer,
walidacja adresu), zmiana statusu wyłącznie przez koordynatora i data zmiany statusu."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from fantazmaty.testing import create_member, create_superuser
from illustrations.models import CoverProposal


class CoverProposalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('cover-admin')
        cls.coordinator = create_member('cover-coordinator', 'Koordynator redakcji')
        cls.member = create_member('cover-member', 'Redaktor')
        cls.outsider = get_user_model().objects.create_user('cover-outsider')
        cls.url = reverse('illustrations:cover_proposal_list')

    def status_url(self, proposal):
        return reverse('illustrations:update_cover_proposal_status', args=[proposal.pk])

    def proposal(self, **fields):
        return CoverProposal.objects.create(illustration_author='Autorka', illustration_url='https://example.test/a',
                                            submitted_by=self.member, **fields)

    def test_member_submits_proposal_with_server_controlled_fields(self):
        self.client.force_login(self.member)
        response = self.client.post(self.url, {
            'illustration_author': 'Anna Okładkowa', 'illustration_url': 'https://example.test/okladka',
            'status': 'approved', 'submitted_by': self.admin.pk,
        })
        self.assertRedirects(response, self.url)
        proposal = CoverProposal.objects.get()
        self.assertEqual((proposal.status, proposal.submitted_by, proposal.status_changed_at),
                         ('pending', self.member, None))
        page = self.client.get(self.url)
        self.assertContains(page, 'Anna Okładkowa')
        self.assertFalse(page.context['can_manage_proposals'])

    def test_invalid_proposal_is_not_saved_and_outsider_has_no_access(self):
        self.client.force_login(self.member)
        response = self.client.post(self.url, {'illustration_author': '', 'illustration_url': 'nie-adres'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context['form'].errors), {'illustration_author', 'illustration_url'})
        self.assertFalse(CoverProposal.objects.exists())
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'illustration_author': 'X'}).status_code, 403)

    def test_only_coordinator_changes_status_and_change_is_dated(self):
        proposal = self.proposal()
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(self.status_url(proposal), {'status': 'approved'}).status_code, 403)
        for user in (self.coordinator, self.admin):
            self.client.force_login(user)
            self.assertTrue(self.client.get(self.url).context['can_manage_proposals'])
        response = self.client.post(self.status_url(proposal), {'status': 'inquiry_sent'}, follow=True)
        self.assertContains(response, 'Status propozycji zmieniono na „Wysłane zapytanie”.')
        proposal.refresh_from_db()
        changed_at = proposal.status_changed_at
        self.assertEqual(proposal.status, 'inquiry_sent')
        self.assertIsNotNone(changed_at)
        self.assertEqual(str(proposal), 'Autorka – Wysłane zapytanie')
        response = self.client.post(self.status_url(proposal), {'status': 'inquiry_sent'}, follow=True)
        self.assertContains(response, 'Propozycja ma już wybrany status.')
        proposal.refresh_from_db()
        self.assertEqual(proposal.status_changed_at, changed_at)

    def test_unknown_status_and_missing_proposal_are_rejected(self):
        proposal = self.proposal()
        self.client.force_login(self.coordinator)
        response = self.client.post(self.status_url(proposal), {'status': 'published'}, follow=True)
        self.assertContains(response, 'Wybrano nieprawidłowy status.')
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'pending')
        missing = reverse('illustrations:update_cover_proposal_status', args=[999999])
        self.assertEqual(self.client.post(missing, {'status': 'approved'}).status_code, 404)
        self.assertEqual(self.client.get(self.status_url(proposal)).status_code, 405)

    def test_model_save_with_update_fields_also_saves_change_date(self):
        proposal = self.proposal()
        proposal.status = 'rejected'
        proposal.save(update_fields=['status'])
        proposal.refresh_from_db()
        self.assertIsNotNone(proposal.status_changed_at)
        proposal.illustration_author = 'Inna'
        proposal.save(update_fields=['illustration_author'])
        self.assertEqual(CoverProposal.objects.get().status_changed_at, proposal.status_changed_at)
