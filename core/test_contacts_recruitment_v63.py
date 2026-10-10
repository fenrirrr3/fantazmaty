from importlib import import_module
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.auth_backends import EmailBackend
from core.models import AudioContributor, Audiobook, Recruitment, RecruitmentMailSource
from core.permissions import is_coordinator, has_role, get_active_person_profile
from core.views.recruitment_notified import notified_token
from people.models import Person, Role
from texts.models import Anthology, Text
from workflow.availability import eligible_role_users
from workflow.models import WorkflowRoleAssignment
from workflow.tests import create_member


class ContactsAndRecruitmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('v63-admin', 'admin63@example.test', 'test')
        cls.coordinator = create_member('v63-editor', 'Koordynator redakcji')
        cls.recruiter = create_member('v63-recruiter', 'Koordynator rekrutacji')
        cls.member = create_member('v63-member', 'Redaktor')
        cls.book = Anthology.objects.create(title='Antologia v63', status='ready')

    def test_contact_history_survives_rename_and_privacy(self):
        contact = AudioContributor.objects.create(name='Czytający', email='private63@example.test', user=self.member)
        for i in range(3):
            text = Text.objects.create(title=f'Nagranie {i}', anthology=self.book, length=1)
            Audiobook.objects.create(text=text, narrator_name=contact.name, narrator_email=contact.email,
                status='published' if i < 2 else 'recording')
        self.assertEqual(AudioContributor.objects.count(), 1)
        contact.name = 'Poprawiony podpis'
        contact.save()
        self.assertEqual(Audiobook.objects.filter(narrator_name=contact.name).count(), 3)
        self.client.force_login(self.member)
        url = reverse('core:audio_contributor', args=[contact.pk])
        page = self.client.get(url)
        self.assertEqual(len(page.context['works']), 2)
        self.assertContains(self.client.get(reverse('core:audiobooks')), url)
        self.assertNotContains(page, 'private63@example.test')
        self.assertContains(self.client.get(reverse('core:person_detail', args=[self.member.person_profile.pk])), url)
        self.client.force_login(self.coordinator)
        self.assertContains(self.client.get(url), 'private63@example.test')
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_suggestions_require_coordinator_and_search_name_or_email(self):
        from illustrations.models import Illustrator
        contact = AudioContributor.objects.create(name='Jan Lektor', email='lekt@example.test')
        Illustrator.objects.create(first_name='Anna', last_name='Rysuje', email='rys@example.test')
        Illustrator.objects.create(first_name='Ukryta', last_name='Osoba', email='old@example.test', is_active=False)
        url = reverse('core:contact_suggestions')
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url, {'kind': 'audio', 'q': 'Jan'}).status_code, 403)
        self.client.force_login(self.coordinator)
        for query in ('Jan', 'lekt@'):
            rows = self.client.get(url, {'kind': 'audio', 'q': query}).json()['results']
            self.assertEqual(rows, [{'id': contact.pk, 'name': 'Jan Lektor', 'email': 'lekt@example.test'}])
        rows = self.client.get(url, {'kind': 'illustration', 'q': 'rys@'}).json()['results']
        self.assertEqual(rows[0]['name'], 'Anna Rysuje')
        self.assertEqual(self.client.get(url, {'kind': 'illustration', 'q': 'old@'}).json()['results'], [])

    def test_contact_account_link_requires_unique_email(self):
        self.member.email = 'contact63@example.test'
        self.member.save(update_fields=['email'])
        contact = AudioContributor.objects.create(name='Lektor', email='CONTACT63@example.test')
        self.assertEqual(contact.user_id, self.member.pk)
        other = AudioContributor.objects.create(name=self.member.get_full_name(), email='')
        self.assertIsNone(other.user_id)

    def test_notified_permission_cannot_be_bypassed_by_post_or_edit_form(self):
        record = Recruitment.objects.create(first_name='Jan', last_name='Kandydat', email='candidate@example.test', department='editors', mail_roles=['editors'])
        url = reverse('core:recruitment_notified', args=[record.pk])
        self.client.force_login(self.coordinator)
        self.assertNotContains(self.client.get(reverse('core:recruitment_list')), 'data-recruitment-notified')
        self.assertEqual(self.client.post(url, {'notified': 'yes', 'version': notified_token(self.coordinator, record)}).status_code, 403)
        record.refresh_from_db()
        self.assertFalse(record.notified)
        from django.contrib import admin
        from core.admin import RecruitmentAdmin
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

    def test_reason_author_changes_only_when_reason_changes_and_download_is_shown(self):
        record = Recruitment.objects.create(mail_roles=['editors', 'verifiers'])
        RecruitmentMailSource.objects.create(recruitment=record, mailbox_key='key', uid=1, uid_validity=1, downloaded_at=timezone.now())
        url = reverse('core:recruitment_detail', args=[record.pk])
        self.client.force_login(self.coordinator)
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

    def test_external_member_visible_but_cannot_login_or_take_work(self):
        person = self.member.person_profile
        person.is_external = True
        person.save()
        person.refresh_from_db()
        self.assertFalse(person.is_active)
        self.assertIsNone(get_active_person_profile(self.member))
        self.assertFalse(EmailBackend().user_can_authenticate(self.member))
        self.assertNotIn(self.member, eligible_role_users(WorkflowRoleAssignment.Role.EDITOR))
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:people_list'))
        self.assertContains(page, 'Zewnętrzny')
        self.assertIn(person, page.context['people'])

    def test_external_admin_profile_can_keep_account_without_email(self):
        from people.admin import PersonAdminForm
        person = self.member.person_profile
        data = {'first_name': person.first_name, 'last_name': person.last_name,
            'user': self.member.pk, 'is_active': 'on', 'is_external': 'on',
            'roles': list(person.roles.values_list('pk', flat=True)), 'email': ''}
        form = PersonAdminForm(data=data, instance=person)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        person.refresh_from_db()
        self.assertTrue(person.is_external)
        self.assertFalse(person.is_active)

    def test_retiring_generic_role_preserves_access_and_revocation(self):
        apps = MigrationExecutor(connection).loader.project_state([('people', '0014_contacts_and_team_status')]).apps
        HistoricalPerson, HistoricalRole = apps.get_model('people', 'Person'), apps.get_model('people', 'Role')
        profile = HistoricalPerson.objects.get(pk=self.member.person_profile.pk)
        generic = HistoricalRole.objects.create(name='Koordynator')
        profile.roles.add(generic)
        import_module('people.migrations.0015_retire_generic_coordinator').retire(apps, SimpleNamespace(connection=connection))
        self.assertFalse(Role.objects.filter(name='Koordynator').exists())
        self.assertTrue(is_coordinator(self.member))
        self.assertTrue(has_role(self.member, 'Koordynator'))
        p = Person.objects.get(pk=profile.pk)
        p.save()
        self.assertFalse(Role.objects.filter(name='Koordynator').exists())
        self.assertIn(self.member, eligible_role_users(WorkflowRoleAssignment.Role.EDITING_COORDINATOR))
        p.is_coordinator = False
        p.save()
        self.assertFalse(is_coordinator(self.member))
        with self.assertRaises(ValidationError):
            Role(name='Koordynator').full_clean()

    def test_existing_audio_contact_migration_is_idempotent_and_preserves_work(self):
        self.member.email = 'migration63@example.test'
        self.member.save(update_fields=['email'])
        apps = MigrationExecutor(connection).loader.project_state([('core', '0030_contacts_and_team_status')]).apps
        Audio = apps.get_model('core', 'Audiobook')
        rows = []
        for i in range(2):
            story = Text.objects.create(title=f'Stare nagranie {i}', anthology=self.book, length=1)
            rows.append(Audio.objects.create(text_id=story.pk, narrator_name='Wspólny lektor',
                narrator_email=self.member.email, status='published', premiere_date='2026-01-02'))
        migrate = import_module('core.migrations.0031_populate_audio_contacts').populate
        for _ in range(2):
            migrate(apps, SimpleNamespace(connection=connection))
        contacts = AudioContributor.objects.filter(name='Wspólny lektor')
        self.assertEqual(contacts.count(), 1)
        self.assertEqual(contacts.get().user_id, self.member.pk)
        for audio in rows:
            audio.refresh_from_db()
            self.assertEqual(audio.narrator_contact_id, contacts.get().pk)
            self.assertEqual(audio.status, 'published')
            self.assertEqual(str(audio.premiere_date), '2026-01-02')

    def test_vocabulary_add_above_tables_and_anthology_has_no_dropdown(self):
        self.client.force_login(self.admin)
        doc = html.fromstring(self.client.get(reverse('core:vocabulary_list')).content)
        for section in doc.xpath('//section[contains(@class,"vocabulary-column")]'):
            add = section.xpath('.//div[contains(@class,"vocabulary-add")]')[0]
            table = section.xpath('.//table')[0]
            self.assertLess(list(section.iter()).index(add), list(section.iter()).index(table))
        page = self.client.get(reverse('core:anthology_list'))
        self.assertNotContains(page, 'class="anthology-details"')
        self.assertContains(page, 'Teksty antologii')
