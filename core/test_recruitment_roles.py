from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from lxml import html
from authors.models import Author
from people.models import Person, Role
from core.models import Recruitment, RecruitmentRoleDecision, MailboxConnection, RecruitmentMailSource
from core.selectors.recruitment import pending_by_role
from core.services.mailbox_import import mailbox_key
from core.services.recruitment_decisions import set_all_decisions


class RecruitmentRoleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = get_user_model().objects.create_superuser('roles-admin', 'admin@example.test', 'test')
        cls.users = {}
        for key, role in [('recruiter', 'Koordynator rekrutacji'), ('coordinator', 'Koordynator redakcji'), ('member', 'Redaktor')]:
            user = get_user_model().objects.create_user(key)
            person = Person.objects.create(user=user, first_name='Jan', last_name=key)
            person.roles.add(Role.objects.get_or_create(name=role)[0])
            cls.users[key] = user
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email='author@example.test')
        cls.box = MailboxConnection.objects.create(name='Próbki', purpose='recruitment', host='imap.example.test', username='test', encrypted_password='unused')

    def setUp(self):
        self.client.force_login(self.users['coordinator'])
        self.record = Recruitment.objects.create(applicant_name='Jan Kandydat', mail_roles=['editors', 'proofreaders', 'editors'], mail_body='Treść https://example.test/probka', notified=True)
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
        self.assertEqual(self.record.status, 'mixed'); self.assertEqual(pending_by_role(), [])
        self.assertContains(self.client.get(url), 'Różne decyzje')
        self.assertContains(self.client.get(url), '>Kto<')
        self.assertEqual(self.client.get(url, {'role':'editors', 'status':'mixed'}).context['page_obj'].paginator.count, 1)

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
        self.record.refresh_from_db(); self.assertEqual(self.record.status, 'rejected')

    def test_migration_preserves_shared_decisions_and_fallback_department(self):
        old = Recruitment.objects.create(mail_roles=['editors', 'reviewers', 'editors'], status='accepted', decision_reason='Dawny powód', unofficial_notes='Dawna notatka')
        legacy = Recruitment.objects.create(department='sound', status='rejected')
        RecruitmentRoleDecision.objects.all().delete()
        module = import_module('core.migrations.0024_recruitment_role_decisions')
        module.copy_existing_decisions(apps, SimpleNamespace(connection=SimpleNamespace(alias='default')))
        self.assertEqual(list(old.role_decisions.values_list('role','status','decision_reason','unofficial_notes')), [('editors','accepted','Dawny powód','Dawna notatka'), ('reviewers','accepted','Dawny powód','Dawna notatka')])
        self.assertEqual(legacy.role_decisions.get().role, 'sound')
        old.refresh_from_db(); self.assertEqual(old.decision_reason, 'Dawny powód')

    def test_author_menu_read_access_without_submission_or_write_access(self):
        for user in (self.users['coordinator'], self.users['recruiter'], self.superuser):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('core:author_list'), {'accepted':'0', 'sort':'email'}).status_code, 200)
            self.assertEqual(self.client.get(reverse('core:author_detail', args=[self.author.pk])).status_code, 200)
            self.assertContains(self.client.get(reverse('core:home')), reverse('core:author_list'))
        self.client.force_login(self.users['coordinator'])
        self.assertEqual(self.client.get(reverse('core:review_bulk_import')).status_code, 403)
        self.client.force_login(self.users['member'])
        self.assertEqual(self.client.get(reverse('core:author_list')).status_code, 403)
        self.assertEqual(self.client.get(reverse('core:author_detail', args=[self.author.pk])).status_code, 403)

    @patch('core.views.recruitment_mailbox.read_headers', return_value={'validity':7,'rows':[{'uid':12,'subject':'Redakcja','sender':'candidate@example.test','date':''}],'total':1,'next_cursor':None,'previous_cursor':None})
    def test_recruiter_mailbox_access_detail_link_and_copy_remains_superuser_only(self, headers):
        RecruitmentMailSource.objects.create(recruitment=self.record, mailbox_key=mailbox_key(self.box), uid_validity=7, uid=12)
        self.client.force_login(self.users['recruiter'])
        url = reverse('core:recruitment_mailbox')
        page = self.client.get(url)
        self.assertContains(page, self.url)
        self.assertNotContains(page, 'Ustawienia skrzynki')
        self.assertContains(self.client.get(reverse('core:home')), url)
        self.assertEqual(self.client.post(url, {'action':'copy_emails', 'selection':page.context['selection'], 'roles':'all', 'date_filter_enabled':'on','sent_since':'2026-10-02'}).status_code, 403)
        from core.test_recruitment_samples import sample
        # A different mail UID can be imported by this role too.
        headers.return_value['rows'][0]['uid'] = 13
        page = self.client.post(url, {'action':'headers', 'roles':'all', 'date_filter_enabled':'on','sent_since':'2026-10-02'}, follow=True)
        with patch('core.views.recruitment_mailbox.fetch_messages', return_value=[sample(13)]):
            self.assertEqual(self.client.post(url, {'action':'add', 'selected':['13'], 'selection':page.context['selection'], 'roles':'all', 'date_filter_enabled':'on','sent_since':'2026-10-02'}).status_code, 302)
        self.assertTrue(RecruitmentMailSource.objects.filter(uid=13).exists())
        for user in (self.users['coordinator'], self.users['member']):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {'action':'add'}).status_code, 403)
        person = self.users['recruiter'].person_profile
        person.is_active = False; person.save()
        self.client.force_login(self.users['recruiter'])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_metadata_edit_does_not_overwrite_role_decisions_and_role_change_stops_old_filter(self):
        self.client.post(self.url, self.payload('editors', 'accepted'))
        self.record.notes = 'Nowe dane'; self.record.save()
        self.assertEqual(self.record.role_decisions.get(role='editors').status, 'accepted')
        manual = Recruitment.objects.create(department='sound', status='accepted')
        manual.department='designers'; manual.save()
        self.assertEqual(manual.status, 'new')
        self.assertEqual(manual.role_decisions.get(role='sound').status, 'accepted')
        url=reverse('core:recruitment_list')
        self.assertEqual(self.client.get(url, {'role':'sound'}).context['page_obj'].paginator.count, 0)

    def test_admin_exposes_role_decisions_link_and_preserves_legacy_fields_readonly(self):
        self.client.force_login(self.superuser)
        page = self.client.get(reverse('admin:core_recruitment_change', args=[self.record.pk]))
        self.assertContains(page, self.url)
        self.assertContains(page, 'Dawne wspólne uzasadnienie')
        self.assertNotContains(page, 'name="decision_reason"')
        self.assertNotContains(page, 'name="status"')
