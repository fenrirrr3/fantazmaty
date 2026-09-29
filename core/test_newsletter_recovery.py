from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse, resolve
from core.models import NewsletterConsent
from core.admin_newsletter_recovery import extract_consent, recover_batch
from core.services.mailbox import MailboxError


def message(text):
    mail=EmailMessage();mail.set_content(text);return mail.as_bytes()


class RecoveryTests(TestCase):
    def test_old_and_new_explicit_consents(self):
        for row in ['Jan Nowak;Tytuł;fantasy;1200;jan@example.com;123;Nabór;premierach, naborach',
                    'Jan Nowak;Tytuł;fantasy;;1200;jan@example.com;123;premierach, naborach;Wiadomość']:
            email, flags=extract_consent(message(row))
            self.assertEqual(email,'jan@example.com');self.assertTrue(all(flags.values()))
        self.assertIsNone(extract_consent(message('Napiszę o premierach i naborach: jan@example.com')))
        row='Jan Nowak;Tytuł;fantasy;;1200;jan@example.com;123;;Piszę o premierach, naborach'
        self.assertFalse(any(extract_consent(message(row))[1].values()))
        self.assertIsNone(extract_consent(message(row+'\n'+row)))
    def test_readonly_peek_idempotent_snapshot(self):
        config=SimpleNamespace(pk=1,host='example.com',port=993,security='ssl',username='teksty@example.com',folder='INBOX',get_password=lambda:'secret')
        raw=message('Jan Nowak;Tytuł;fantasy;1200;jan@example.com;123;Nabór;premierach')
        client=MagicMock();client.select.return_value=('OK',[b'1']);client.response.return_value=('UIDVALIDITY',[b'10'])
        def uid(command,*args):
            if command=='SEARCH':return 'OK',[b'1']
            if args[-1]=='(UID RFC822.SIZE)':return 'OK',[f'1 (UID 1 RFC822.SIZE {len(raw)})'.encode()]
            return 'OK',[(b'1 (UID 1 BODY[] {123}',raw),b')']
        client.uid.side_effect=uid
        with patch('core.admin_newsletter_recovery.imaplib.IMAP4_SSL',return_value=client):
            state=recover_batch(config)
            recover_batch(config)
        self.assertTrue(state['done']);self.assertEqual(state['matched'],1)
        self.assertEqual(NewsletterConsent.objects.count(),1)
        client.select.assert_called_with('"INBOX"',readonly=True)
        self.assertTrue(any('BODY.PEEK' in str(c) for c in client.uid.call_args_list))
        self.assertFalse(any(c.args[0] in ('STORE','EXPUNGE') for c in client.uid.call_args_list))
        client.response.return_value=('UIDVALIDITY',[b'11'])
        with patch('core.admin_newsletter_recovery.imaplib.IMAP4_SSL',return_value=client):
            with self.assertRaises(MailboxError): recover_batch(config,state)

    def test_admin_only_no_get_write_and_csrf(self):
        admin=get_user_model().objects.create_superuser('admin','admin@example.com','pass')
        user=get_user_model().objects.create_user('staff',is_staff=True)
        url=reverse('admin:core_mailbox_recover_newsletters')
        self.client.force_login(user);self.assertIn(self.client.get(url).status_code,(302,403))
        self.client.force_login(admin)
        with patch('core.admin_newsletter_recovery.recover_batch') as action:
            self.assertEqual(self.client.get(url).status_code,200);action.assert_not_called()
        client=Client(enforce_csrf_checks=True);client.force_login(admin)
        self.assertEqual(client.post(url).status_code,403)
    def test_canonical_routes_and_old_aliases(self):
        self.assertEqual(reverse('core:workflow_list'),'/tabelka-zbiorcza/')
        self.assertEqual(resolve('/etapy-prac/').func,resolve('/tabelka-zbiorcza/').func)
        self.assertEqual(reverse('core:dashboard_tasks'),'/pulpit/zadania/')
        self.assertIn('/skrzynki-zgloszen/',reverse('admin:core_mailboxconnection_changelist'))
        self.assertIn('/popraw/',reverse('admin:workflow_stage_correct',args=[1]))
