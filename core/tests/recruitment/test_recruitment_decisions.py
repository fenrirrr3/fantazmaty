"""Szczegóły zgłoszenia rekrutacyjnego: niezależne decyzje dla każdej roli, autor uzasadnienia, flaga „powiadomiony”, usuwanie zgłoszenia i ochrona przed nieaktualną edycją."""
from unittest.mock import patch

from django.contrib import admin
from django.db import transaction
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.admin import RecruitmentAdmin
from core.models import Recruitment, RecruitmentMailSource
from core.selectors.recruitment import pending_by_role
from core.services.recruitment_decisions import set_all_decisions
from core.views.recruitment_delete import deletion_token
from core.views.recruitment_notified import notified_token

from .support import FORM_BODY, RecruitmentTeamMixin


class RecruitmentDetailTests(RecruitmentTeamMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.record = Recruitment.objects.create(
            applicant_name='Maria Anna Nowak-Kowalska', email='candidate@example.test', mail_body=FORM_BODY,
            mail_subject='Nowe zgłoszenie', mail_roles=['illustrators'], notes='Uwagi istniejące', notified=True)

    def setUp(self):
        self.url = reverse('core:recruitment_detail', args=[self.record.pk])

    def test_detail_links_safe_body_and_theme_buttons_together(self):
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            self.assertContains(self.client.get(reverse('core:recruitment_list')), self.url)
            response = self.client.get(self.url)
            self.assertContains(response, 'Maria Anna Nowak-Kowalska')
            self.assertContains(response, 'Uwagi istniejące')
            doc = html.fromstring(response.content)
            links = doc.xpath('//div[contains(@class,"recruitment-message")]//a/@href')
            self.assertIn('https://example.test/portfolio', links)
            self.assertIn('https://example.test/probka?x=1&y=2', links)
            self.assertFalse(any(link.startswith('javascript:') for link in links))
            self.assertFalse(doc.xpath('//div[contains(@class,"recruitment-message")]//script'))
            self.assertEqual(len(doc.xpath('//div[contains(@class,"theme-buttons")]/button')), 2)

    def test_privileged_decision_and_reason_only_no_notification_or_metadata_overwrite(self):
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            version = self.client.get(self.url).context['sections'][0]['version']
            previous_date = Recruitment.objects.get(pk=self.record.pk).notified_at
            with patch('django.core.mail.send_mail') as send:
                response = self.client.post(self.url, {
                    'role': 'illustrators', 'version': version, 'illustrators-status': 'rejected',
                    'illustrators-decision_reason': 'Uzasadnienie decyzji', 'notified': '', 'mail_body': 'Zmiana',
                    'email': 'other@example.test'})
                self.assertRedirects(response, self.url)
                send.assert_not_called()
            record = Recruitment.objects.get(pk=self.record.pk)
            self.assertEqual(record.status, 'rejected')
            self.assertEqual(record.role_decisions.get(role='illustrators').decision_reason, 'Uzasadnienie decyzji')
            self.assertEqual(record.mail_body, FORM_BODY)
            self.assertEqual(record.email, 'candidate@example.test')
            self.assertTrue(record.notified)
            self.assertEqual(record.notified_at, previous_date)
            self.assertEqual(record.notes, 'Uwagi istniejące')

    def test_no_decision_invalid_status_and_stale_edit(self):
        """Nieaktualna wersja formularza daje 409 i zachowuje wpisane uzasadnienie zamiast je gubić."""
        self.client.force_login(self.coordinator)
        version = self.client.get(self.url).context['sections'][0]['version']
        response = self.client.post(self.url, {'role': 'illustrators', 'version': version, 'illustrators-status': 'bad'})
        self.assertEqual(response.status_code, 400)
        with transaction.atomic():
            record = Recruitment.objects.select_for_update().get(pk=self.record.pk)
            set_all_decisions(record, 'accepted')
        response = self.client.post(self.url, {'role': 'illustrators', 'version': version, 'illustrators-status': 'rejected',
                                               'illustrators-decision_reason': 'Nie zgub szkicu'})
        self.assertContains(response, 'Nie zgub szkicu', status_code=409)
        record.refresh_from_db()
        self.assertEqual(record.status, 'accepted')
        version = self.client.get(self.url).context['sections'][0]['version']
        response = self.client.post(self.url, {'role': 'illustrators', 'version': version, 'illustrators-status': 'new'})
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertIsNone(record.accepted)

    def test_permissions_and_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'status': 'accepted'}).status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.coordinator)
        self.assertEqual(client.post(self.url, {'status': 'accepted'}).status_code, 403)


class RoleDecisionTests(RecruitmentTeamMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.coordinator)
        self.record = Recruitment.objects.create(applicant_name='Jan Kandydat', mail_roles=['editors', 'proofreaders', 'editors'],
                                                 mail_body='Treść https://example.test/probka', notified=True)
        self.url = reverse('core:recruitment_detail', args=[self.record.pk])

    def payload(self, role, status, version=None, **extra):
        row = self.record.role_decisions.get(role=role)
        return {'role': role, 'version': version or row.updated_at.isoformat(), f'{role}-status': status,
                f'{role}-decision_reason': 'Powód ' + role, f'{role}-unofficial_notes': 'Notatka ' + role, **extra}

    def test_independent_forms_pending_counts_and_mixed_summary(self):
        doc = html.fromstring(self.client.get(self.url).content)
        sections = doc.xpath('//details[contains(@class,"recruitment-role-decision")]')
        self.assertEqual(len(sections), 2)
        self.assertFalse(any('open' in node.attrib for node in sections))
        self.assertEqual(len(doc.xpath('//form[@data-warn-unsaved]')), 2)
        self.assertEqual(self.client.post(self.url, self.payload('editors', 'accepted')).status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'new')
        self.assertEqual(self.record.decision_display, 'nie wszystkie decyzje')
        self.assertTrue(self.record.notified)
        proof = self.record.role_decisions.get(role='proofreaders')
        self.assertEqual((proof.status, proof.decision_reason, proof.unofficial_notes), ('new', '', ''))
        self.assertEqual(pending_by_role(), [{'role': 'proofreaders', 'label': 'Korekta', 'count': 1}])
        url = reverse('core:recruitment_list')
        self.assertEqual(self.client.get(url, {'role': 'editors', 'status': 'new'}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(url, {'role': 'editors', 'status': 'accepted'}).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.client.post(self.url, self.payload('proofreaders', 'rejected')).status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'mixed')
        self.assertEqual(pending_by_role(), [])
        page = self.client.get(url)
        self.assertContains(page, 'Różne decyzje')
        self.assertContains(page, '>Kto<')
        self.assertEqual(self.client.get(url, {'role': 'editors', 'status': 'mixed'}).context['page_obj'].paginator.count, 1)

    def test_concurrent_different_roles_allowed_same_role_conflict_and_wrong_role_rejected(self):
        first = self.payload('editors', 'accepted')
        other = self.payload('proofreaders', 'rejected')
        self.assertEqual(self.client.post(self.url, first).status_code, 302)
        self.assertEqual(self.client.post(self.url, other).status_code, 302)
        first['editors-status'] = 'rejected'
        self.assertEqual(self.client.post(self.url, first).status_code, 409)
        self.assertEqual(self.record.role_decisions.get(role='editors').status, 'accepted')
        self.assertEqual(self.client.post(self.url, {**first, 'role': 'reviewers'}).status_code, 400)
        self.assertEqual(self.client.post(self.url, self.payload('editors', 'mixed')).status_code, 400)

    def test_single_role_collapsed_and_bulk_preserves_notes_invalidates_forms(self):
        one = Recruitment.objects.create(department='sound')
        doc = html.fromstring(self.client.get(reverse('core:recruitment_detail', args=[one.pk])).content)
        self.assertEqual(len(doc.xpath('//details[contains(@class,"recruitment-role-decision")]')), 1)
        self.assertFalse(doc.xpath('//details[contains(@class,"recruitment-role-decision")][@open]'))
        self.client.post(self.url, self.payload('editors', 'accepted'))
        stale = self.payload('editors', 'accepted')
        with transaction.atomic():
            record = Recruitment.objects.select_for_update().get(pk=self.record.pk)
            set_all_decisions(record, 'rejected')
        row = self.record.role_decisions.get(role='editors')
        self.assertEqual((row.decision_reason, row.unofficial_notes), ('Powód editors', 'Notatka editors'))
        self.assertEqual(self.client.post(self.url, stale).status_code, 409)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, 'rejected')

    def test_metadata_edit_does_not_overwrite_role_decisions_and_role_change_stops_old_filter(self):
        self.client.post(self.url, self.payload('editors', 'accepted'))
        self.record.notes = 'Nowe dane'
        self.record.save()
        self.assertEqual(self.record.role_decisions.get(role='editors').status, 'accepted')
        manual = Recruitment.objects.create(department='sound', status='accepted')
        manual.department = 'designers'
        manual.save()
        self.assertEqual(manual.status, 'new')
        self.assertEqual(manual.role_decisions.get(role='sound').status, 'accepted')
        url = reverse('core:recruitment_list')
        self.assertEqual(self.client.get(url, {'role': 'sound'}).context['page_obj'].paginator.count, 0)

    def test_reason_author_changes_only_when_reason_changes_and_download_is_shown(self):
        record = Recruitment.objects.create(mail_roles=['editors', 'verifiers'])
        RecruitmentMailSource.objects.create(recruitment=record, mailbox_key='key', uid=1, uid_validity=1,
                                             downloaded_at=timezone.now())
        url = reverse('core:recruitment_detail', args=[record.pk])
        self.assertContains(self.client.get(url), 'Załączniki pobrane:')
        reason = record.role_decisions.get(role='editors')
        data = {'role': 'editors', 'version': reason.updated_at.isoformat(), 'editors-status': 'accepted',
                'editors-decision_reason': 'Dobre próbki', 'editors-unofficial_notes': ''}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        reason.refresh_from_db()
        self.assertEqual(reason.reason_author, self.coordinator)
        self.client.force_login(self.recruiter)
        data.update(version=reason.updated_at.isoformat(), **{'editors-unofficial_notes': 'Tylko notatka'})
        self.assertEqual(self.client.post(url, data).status_code, 302)
        reason.refresh_from_db()
        self.assertEqual(reason.reason_author, self.coordinator)
        data.update(version=reason.updated_at.isoformat(), **{'editors-decision_reason': 'Nowe uzasadnienie'})
        self.assertEqual(self.client.post(url, data).status_code, 302)
        reason.refresh_from_db()
        self.assertEqual(reason.reason_author, self.recruiter)
        self.assertIsNone(record.role_decisions.get(role='verifiers').reason_author)

    def test_admin_exposes_role_decisions_link_and_preserves_legacy_fields_readonly(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('admin:core_recruitment_change', args=[self.record.pk]))
        self.assertContains(page, self.url)
        self.assertContains(page, 'Dawne wspólne uzasadnienie')
        self.assertNotContains(page, 'name="decision_reason"')
        self.assertNotContains(page, 'name="status"')


class RecruitmentNotifiedTests(RecruitmentTeamMixin, TestCase):
    def setUp(self):
        self.record = Recruitment.objects.create(mail_roles=['editors', 'proofreaders'], notes='Nie zmieniaj',
                                                 unofficial_notes='Prywatne')
        self.url = reverse('core:recruitment_notified', args=[self.record.pk])

    def test_notified_permission_cannot_be_bypassed_by_post_or_edit_form(self):
        record = Recruitment.objects.create(first_name='Jan', last_name='Kandydat', email='candidate@example.test',
                                            department='editors', mail_roles=['editors'])
        url = reverse('core:recruitment_notified', args=[record.pk])
        self.client.force_login(self.coordinator)
        self.assertNotContains(self.client.get(reverse('core:recruitment_list')), 'data-recruitment-notified')
        self.assertEqual(self.client.post(url, {'notified': 'yes', 'version': notified_token(self.coordinator, record)}).status_code, 403)
        record.refresh_from_db()
        self.assertFalse(record.notified)
        request = RequestFactory().get('/')
        request.user = self.coordinator
        self.assertIn('notified', RecruitmentAdmin(Recruitment, admin.site).get_readonly_fields(request, record))
        edit_url = reverse('core:recruitment_edit', args=[record.pk])
        form = self.client.get(edit_url).context['form']
        data = {name: form[name].value() or '' for name in form.fields}
        data.update(notified='on', version=record.updated_at.isoformat())
        response = self.client.post(edit_url, data)
        self.assertEqual(response.status_code, 302, response.context['form'].errors if response.status_code == 400 else '')
        record.refresh_from_db()
        self.assertFalse(record.notified)
        for user in (self.recruiter, self.admin):
            self.client.force_login(user)
            self.assertEqual(self.client.post(url, {'notified': 'yes', 'version': notified_token(user, record)}).status_code, 200)
            record.refresh_from_db()
            self.assertTrue(record.notified)

    def test_only_flag_changes_and_stale_or_forged_tokens_rejected(self):
        self.client.force_login(self.recruiter)
        token = notified_token(self.recruiter, self.record)
        fields = ('role', 'status', 'decision_reason', 'unofficial_notes')
        before = list(self.record.role_decisions.values(*fields))
        response = self.client.post(self.url, {'notified': 'yes', 'version': token})
        self.assertEqual(response.status_code, 200)
        self.record.refresh_from_db()
        date = self.record.notified_at
        self.assertEqual(self.record.notes, 'Nie zmieniaj')
        self.assertEqual(list(self.record.role_decisions.values(*fields)), before)
        self.assertEqual(self.client.post(self.url, {'notified': 'no', 'version': token}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'notified': 'yes', 'version': 'forged'}).status_code, 409)
        version = response.json()['version']
        self.assertEqual(self.client.post(self.url, {'notified': 'invalid', 'version': version}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'notified': 'yes', 'version': version}).status_code, 200)
        self.record.refresh_from_db()
        self.assertEqual(self.record.notified_at, date)

    def test_permissions_csrf_and_post_only(self):
        self.client.force_login(self.recruiter)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.recruiter)
        self.assertEqual(csrf.post(self.url, {'notified': 'yes'}).status_code, 403)
        self.recruiter.person_profile.roles.clear()
        self.assertEqual(self.client.post(self.url, {'notified': 'yes'}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {'notified': 'yes'}).status_code, 302)


class RecruitmentDeletionTests(RecruitmentTeamMixin, TestCase):
    def setUp(self):
        self.record = Recruitment.objects.create(first_name='Jan', last_name='Kandydat', mail_roles=['editors', 'proofreaders'])
        RecruitmentMailSource.objects.create(recruitment=self.record, mailbox_key='key', uid_validity=1, uid=2)
        self.url = reverse('core:recruitment_delete', args=[self.record.pk])
        self.detail = reverse('core:recruitment_detail', args=[self.record.pk])

    def test_only_superuser_sees_button_and_can_delete(self):
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.client.force_login(self.coordinator)
        self.assertNotContains(self.client.get(self.detail), 'Usuń zgłoszenie')
        for method in ('get', 'post'):
            self.assertEqual(getattr(self.client, method)(self.url).status_code, 403)
        self.client.force_login(self.admin)
        page = self.client.get(self.detail)
        self.assertContains(page, 'cms-danger-button')
        self.assertContains(page, 'Odśwież dane')
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertTrue(Recruitment.objects.filter(pk=self.record.pk).exists())
        other = Recruitment.objects.create(first_name='Inna osoba')
        result = self.client.post(self.url, {'version': deletion_token(self.admin, self.record)})
        self.assertRedirects(result, reverse('core:recruitment_list'))
        self.assertFalse(Recruitment.objects.filter(pk=self.record.pk).exists())
        self.assertFalse(RecruitmentMailSource.objects.exists())
        self.assertFalse(self.record.role_decisions.exists())
        self.assertTrue(Recruitment.objects.filter(pk=other.pk).exists())

    def test_csrf_and_newer_decision_prevent_deletion(self):
        self.client.force_login(self.admin)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(self.url).status_code, 403)
        token = deletion_token(self.admin, self.record)
        decision = self.record.role_decisions.first()
        decision.decision_reason = 'Nowe uzasadnienie'
        decision.save()
        self.assertEqual(self.client.post(self.url, {'version': token}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'version': 'forged'}).status_code, 409)
        self.assertTrue(Recruitment.objects.filter(pk=self.record.pk).exists())
