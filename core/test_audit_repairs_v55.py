from email.message import EmailMessage
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.models import Recruitment, MailboxConnection
from core.selectors.recruitment import pending_by_role
from core.services.mailbox import read_headers
from core.services.mailbox_email_copy import sender_emails
from illustrations.editing import AssignmentForm, edit_token
from illustrations.models import Illustration, Illustrator, PublicIllustrationSettings
from illustrations.public import link_version
from texts.models import Anthology, Text


class AuditAdminRepairsTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('repair-admin', 'admin@example.test', 'test')
        self.client.force_login(self.admin)
        self.book = Anthology.objects.create(title='Książka', has_illustrations=True)
        self.text = Text.objects.create(title='Tekst', anthology=self.book, length=10)
        self.row = Illustration.objects.get(text=self.text)

    def token(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return html.fromstring(response.content).xpath('//input[@name="_edit_version"]/@value')[0]

    def test_inactive_new_assignment_blocked_both_forms_existing_credit_retained(self):
        artist = Illustrator.objects.create(first_name='Nieaktywny', is_active=False)
        form = AssignmentForm(data={'illustrators': [artist.pk], 'status': 'assigned', 'version': edit_token(self.admin, self.row)}, instance=self.row, can_assign=True)
        self.assertFalse(form.is_valid())
        self.assertIn('illustrators', form.errors)
        url = reverse('admin:illustrations_illustration_change', args=[self.row.pk])
        response = self.client.post(url, {'_edit_version': self.token(url), 'text': self.text.pk, 'illustrators': [artist.pk], 'status': 'assigned', '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('illustrators', response.context['adminform'].form.errors)
        self.assertFalse(self.row.illustrators.exists())
        self.row.set_artists([artist], status='assigned')  # Historical credit.
        response = self.client.post(url, {'_edit_version': self.token(url), 'text': self.text.pk, 'illustrators': [artist.pk], 'status': 'delivered', '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'delivered')
        self.assertEqual(list(self.row.illustrators.all()), [artist])
        found = self.client.get(reverse('admin:autocomplete'), {'app_label': 'illustrations', 'model_name': 'illustration', 'field_name': 'illustrators', 'term': 'Nieaktywny'})
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()['results'], [])

    def test_drive_conflicts_between_admin_and_website_and_two_admin_forms(self):
        old = 'https://drive.google.com/drive/folders/old'
        new = 'https://drive.google.com/drive/folders/new'
        last = 'https://drive.google.com/drive/folders/last'
        settings = PublicIllustrationSettings.objects.create(drive_url=old)
        url = reverse('admin:illustrations_publicillustrationsettings_change', args=[1])
        token = self.token(url)
        version = version_of(settings)
        internal = reverse('illustrations:illustration_list')
        self.assertEqual(self.client.post(internal, {'drive_url': new, 'version': link_version(self.admin, old)}).status_code, 302)
        self.assertGreater(version_of(settings), version)
        self.assertEqual(self.client.post(url, {'_edit_version': token, 'drive_url': old, '_save': 'Zapisz'}).status_code, 409)
        settings.refresh_from_db(); self.assertEqual(settings.drive_url, new)
        token = self.token(url)
        self.assertEqual(self.client.post(url, {'_edit_version': token, 'drive_url': last, '_save': 'Zapisz'}).status_code, 302)
        self.assertEqual(self.client.post(url, {'_edit_version': token, 'drive_url': old, '_save': 'Zapisz'}).status_code, 409)
        self.assertEqual(self.client.post(internal, {'drive_url': old, 'version': link_version(self.admin, new)}).status_code, 409)
        settings.refresh_from_db(); self.assertEqual(settings.drive_url, last)

    def test_admin_roles_sync_archive_notes_reactivate_and_reject_stale_role_form(self):
        record = Recruitment.objects.create(first_name='Jan', last_name='Kandydat', email='jan@example.test', mail_roles=['editors'], mail_fingerprint='a' * 64)
        decision = record.role_decisions.get(role='editors')
        decision.status = 'accepted'; decision.decision_reason = 'Wcześniejsze uzasadnienie'; decision.unofficial_notes = '<script>prywatne</script>'; decision.save()
        record.save()
        old_version = decision.updated_at.isoformat()
        url = reverse('admin:core_recruitment_change', args=[record.pk])
        payload = {'first_name': 'Jan', 'last_name': 'Kandydat', 'email': record.email, 'submitted_at': '2026-10-08', '_save': 'Zapisz'}
        page = self.client.get(url)
        self.assertNotIn('department', page.context['adminform'].form.fields)
        for roles in (['proofreaders', 'promotion'], ['editors', 'proofreaders', 'sound']):
            response = self.client.post(url, {**payload, 'selected_roles': roles, '_edit_version': self.token(url)})
            self.assertEqual(response.status_code, 302)
            record.refresh_from_db()
            self.assertCountEqual(record.mail_roles, roles)
            self.assertEqual(record.status, 'new')
            self.assertEqual(record.role_decisions.get(role='editors').decision_reason, 'Wcześniejsze uzasadnienie')
            if 'editors' not in roles:
                page = self.client.get(url)
                self.assertContains(page, 'Wcześniejsze uzasadnienie')
                self.assertContains(page, '&lt;script&gt;prywatne&lt;/script&gt;')
                self.assertEqual({r['role'] for r in pending_by_role()}, {'proofreaders', 'promotion'})
        self.assertEqual(record.role_decisions.get(role='editors').status, 'accepted')
        self.assertIn('Dźwiękowcy', record.mail_roles_display)
        detail = reverse('core:recruitment_detail', args=[record.pk])
        self.assertEqual(self.client.post(detail, {'role': 'editors', 'version': old_version, 'editors-status': 'rejected'}).status_code, 409)
        page = self.client.get(reverse('admin:core_recruitment_changelist'), {'recruitment_role': 'sound'})
        self.assertEqual([row.pk for row in page.context['cl'].result_list], [record.pk])
        self.assertContains(page, 'Dźwiękowcy')
        response = self.client.post(url, {**payload, '_edit_version': self.token(url)})
        self.assertEqual(response.status_code, 200)
        self.assertIn('selected_roles', response.context['adminform'].form.errors)
        record.refresh_from_db(); self.assertCountEqual(record.mail_roles, ['editors', 'proofreaders', 'sound'])

    def test_admin_can_add_multiple_roles_and_invalid_roles_are_rejected(self):
        url = reverse('admin:core_recruitment_add')
        payload = {'first_name': 'Nowa', 'last_name': 'Osoba', 'email': 'new@example.test', 'submitted_at': '2026-10-08', '_save': 'Zapisz'}
        self.assertEqual(self.client.post(url, {**payload, 'selected_roles': ['editors', 'fake']}).status_code, 200)
        self.assertFalse(Recruitment.objects.exists())
        self.assertEqual(self.client.post(url, {**payload, 'selected_roles': ['editors', 'illustrators']}).status_code, 302)
        record = Recruitment.objects.get()
        self.assertCountEqual(record.role_decisions.values_list('role', flat=True), ['editors', 'illustrators'])
        self.assertTrue(all(row.status == 'new' for row in record.role_decisions.all()))

    def test_legacy_edit_url_changes_actual_roles_and_checks_version(self):
        record = Recruitment.objects.create(first_name='Jan', last_name='Kandydat', email='jan@example.test', mail_roles=['editors'], mail_fingerprint='b' * 64)
        url = reverse('core:recruitment_edit', args=[record.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('department', page.context['form'].fields)
        payload = {'first_name': 'Jan', 'last_name': 'Kandydat', 'email': record.email, 'submitted_at': '2026-10-08', 'selected_roles': ['proofreaders', 'illustrators'], 'version': record.updated_at.isoformat()}
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.mail_roles, ['proofreaders', 'illustrators'])
        self.assertCountEqual(record.role_decisions.values_list('role', flat=True), ['editors', 'proofreaders', 'illustrators'])
        self.assertEqual(self.client.post(url, payload).status_code, 409)

    def test_duplicate_alias_blocked_on_both_forms_and_full_name_collision(self):
        artist = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz', pseudonym='Graphos')
        payload = {'first_name': 'Inny', 'last_name': 'Artysta', 'pseudonym': '  gRaPhOs  ', 'is_active': 'on'}
        for url in (reverse('illustrations:illustrator_add'), reverse('admin:illustrations_illustrator_add')):
            result = self.client.post(url, payload)
            self.assertEqual(result.status_code, 200)
            self.assertContains(result, f'ID {artist.pk}')
        self.assertEqual(Illustrator.objects.count(), 1)
        result = self.client.post(reverse('illustrations:illustrator_add'), {'first_name': 'Graphos', 'last_name': '', 'pseudonym': '', 'is_active': 'on'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Illustrator.objects.count(), 1)
        Illustrator.objects.create(first_name='Drugi', last_name='Artysta')
        result = self.client.post(reverse('illustrations:illustrator_add'), {**payload, 'pseudonym': 'Drugi Artysta'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Illustrator.objects.count(), 2)

    def test_legacy_duplicate_labels_have_ids_and_can_edit_contacts_then_merge(self):
        artist = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz', pseudonym='Graphos')
        duplicate = Illustrator.objects.create(first_name='Graphos')
        page = self.client.get(reverse('illustrations:illustrator_list'))
        for person in (artist, duplicate):
            self.assertContains(page, f'Graphos (ID {person.pk})')
        form = AssignmentForm(instance=self.row, can_assign=True)
        self.assertEqual(form.fields['illustrators'].label_from_instance(artist), f'Graphos (ID {artist.pk})')
        url = reverse('admin:illustrations_illustrator_change', args=[artist.pk])
        response = self.client.post(url, {'_edit_version': self.token(url), 'first_name': 'Przemek', 'last_name': 'Świszcz', 'pseudonym': 'Graphos', 'email': 'updated@example.test', 'is_active': 'on'})
        self.assertEqual(response.status_code, 302)
        call_command('merge_illustrator', first_name='Przemek', last_name='Świszcz', pseudonym='Graphos', apply=True, stdout=StringIO())
        self.assertEqual(Illustrator.objects.count(), 1)
        self.assertNotContains(self.client.get(reverse('illustrations:illustrator_list')), '(ID ')


class ExactRecruitmentFilterTests(TestCase):
    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_exact_roles_counts_pagination_copy_multi_role_and_all(self, imap):
        box = MailboxConnection.objects.create(name='Próbki', purpose='recruitment', host='imap.example.test', username='audit', encrypted_password='unused')
        client = imap.return_value
        client.select.return_value = ('OK', [b'240'])
        client.response.return_value = ('UIDVALIDITY', [b'7'])
        subjects = {n: ['Korekta audiobooków', 'Korekta', 'Korekta poskładowa', 'Korekta, Korekta audiobooków'][n % 4] for n in range(1, 241)}
        fetch_sizes = []
        def uid(command, *args):
            if command == 'SEARCH':
                return 'OK', [' '.join(map(str, subjects)).encode()]
            numbers = list(map(int, args[0].split(',')))
            self.assertLessEqual(len(numbers), 200)
            self.assertIn('BODY.PEEK[HEADER.FIELDS', args[1])
            fetch_sizes.append(len(numbers))
            result = []
            for number in numbers:
                message = EmailMessage()
                message['Subject'] = 'Rekrutacja – Jan Kandydat – ' + subjects[number]
                message['From'] = f'person{number}@example.test'
                result.append((f'1 (UID {number} BODY[])'.encode(), message.as_bytes()))
            return 'OK', result
        client.uid.side_effect = uid
        with patch.object(box, 'get_password', return_value='test'):
            first = read_headers(box, recruitment_roles=['proofreaders'])
            self.assertEqual(first['total'], 120)
            self.assertEqual(len(first['rows']), 50)
            second = read_headers(box, first['next_cursor'], recruitment_roles=['proofreaders'])
            third = read_headers(box, second['next_cursor'], recruitment_roles=['proofreaders'])
            self.assertEqual(len(third['rows']), 20)
            self.assertIsNone(third['next_cursor'])
            self.assertEqual({r['uid'] for page in (first, second, third) for r in page['rows']}, {n for n in subjects if n % 2})
            back = read_headers(box, second['previous_cursor'], recruitment_roles=['proofreaders'])
            self.assertEqual(back['rows'], first['rows'])
            copied = sender_emails(box, 7, recruitment_roles=['proofreaders'])
            self.assertEqual(set(copied), {f'person{n}@example.test' for n in subjects if n % 2})
            all_rows = read_headers(box, recruitment_roles=[])
            self.assertEqual(all_rows['total'], 240)
            union = read_headers(box, recruitment_roles=['proofreaders', 'audio_proofreaders'])
            self.assertEqual(union['total'], 180)
            excluded = read_headers(box, recruitment_roles=['proofreaders'], excluded=lambda validity: {1, 3})
            self.assertEqual(excluded['total'], 118)
        client.store.assert_not_called(); client.expunge.assert_not_called()

    @patch('core.services.mailbox.imaplib.IMAP4_SSL')
    def test_audio_only_does_not_leave_empty_pages_or_false_count(self, imap):
        box = MailboxConnection.objects.create(name='Próbki', purpose='recruitment', host='imap.example.test', username='audit', encrypted_password='unused')
        client = imap.return_value
        client.select.return_value = ('OK', [b'1']); client.response.return_value = ('UIDVALIDITY', [b'7'])
        message = EmailMessage(); message['Subject'] = 'Korekta audiobooków'
        client.uid.side_effect = [('OK', [b'1']), ('OK', [(b'1 (UID 1 BODY[])', message.as_bytes())])]
        with patch.object(box, 'get_password', return_value='test'):
            result = read_headers(box, recruitment_roles=['proofreaders'])
        self.assertEqual(result, {'validity': 7, 'rows': [], 'total': 0, 'next_cursor': None, 'previous_cursor': None})
