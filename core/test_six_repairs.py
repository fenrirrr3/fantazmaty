from datetime import date
from email.message import EmailMessage
from django.test import TestCase, SimpleTestCase
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError
from texts.models import Text, Anthology
from people.models import Person
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.import_context import importing_completed
from workflow.services import resume_editing, _ensure_editing_phase
from core.management.commands.repair_ballady_import import repair, SOURCE, BOOK, TARGETS
from core.supervision import integrity_issues, text_credit_groups
from core.services.mailbox_import import parse_message
from core.services.mailbox import MailboxError
from core.services.review_import_parser import parse_review_records

class RepairBalladyTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_superuser('editor','editor@example.com','password')
        self.book=Anthology.objects.create(title=BOOK)
        token=importing_completed.set(True)
        try:
            for row,title,end in TARGETS:
                t=Text.objects.create(title=title,length=123,anthology=self.book,import_source=SOURCE,import_source_row=row)
                for n in range(1,4 if row==1 else 2):
                    a=A.objects.create(text=t,role='editor',assigned_to=self.user,execution_number=n,is_current=n==(3 if row==1 else 1))
                    S.objects.create(text=t,stage_type='editing',assignment=a,iteration=n,execution_number=n,is_current=a.is_current,is_completed=True,imported_completed=True)
                editor=a
                for role,kind in [('editing_coordinator','editing_control'),('proofreader_1','first_proofreading'),('verifier_1','first_verification')]:
                    a=A.objects.create(text=t,role=role,assigned_to=self.user)
                    S.objects.create(text=t,stage_type=kind,assignment=a,is_completed=True,imported_completed=True,started_at=end if kind=='first_verification' else None,ended_at=end if kind=='first_verification' else None)
                S.objects.create(text=t,stage_type='author_editing',assignment=editor)
        finally:importing_completed.reset(token)

    def test_preview_repair_repeat_and_resume(self):
        counts=(Text.objects.count(),A.objects.count(),S.objects.count())
        repair();self.assertFalse(S.objects.filter(stage_type='author_editing',started_at__isnull=False).exists())
        repair(apply=True);repair(apply=True)
        self.assertEqual(counts,(Text.objects.count(),A.objects.count(),S.objects.count()))
        for _,title,end in TARGETS:
            text=Text.objects.get(title=title)
            self.assertEqual(S.objects.get(text=text,stage_type='author_editing').started_at,end)
            _ensure_editing_phase(text)
            stage=resume_editing(text,self.user,started_at=date(2026,9,29))
            self.assertEqual(stage.stage_type,'editing');self.assertFalse(stage.is_completed)
        repair(apply=True) # No resetting of subsequent legitimate work.

    def test_changed_second_text_blocks_all_writes(self):
        S.objects.filter(text__title=TARGETS[1][1],stage_type='author_editing').update(started_at=date(2026,9,25))
        with self.assertRaises(CommandError):repair(apply=True)
        self.assertIsNone(S.objects.get(text__title=TARGETS[0][1],stage_type='author_editing').started_at)
        self.assertTrue(S.objects.get(text__title=TARGETS[0][1],stage_type='first_proofreading').is_current)

class CreditAndIntegrityTests(TestCase):
    def test_skip_is_not_corrupt_and_homonyms_not_merged(self):
        book=Anthology.objects.create(title='Test');t=Text.objects.create(title='Test',anthology=book,length=10)
        S.objects.create(text=t,stage_type='fourth_proofreading',is_completed=True,is_skipped=True)
        S.objects.create(text=t,stage_type='styling')
        self.assertFalse(any(x['label']=='Sprzeczny zapis zakończenia' for x in integrity_issues()))
        token=importing_completed.set(True)
        try:
            for n,(first,last) in enumerate([('Zofia','Alfa'),('Adam','Zeta'),('Zofia','Alfa'),('','')],1):
                u=get_user_model().objects.create_user(f'secret{n}@example.com',email=f'secret{n}@example.com',first_name=first,last_name=last)
                if first:Person.objects.create(user=u,email=u.email,first_name=first,last_name=last)
                a=A.objects.create(text=t,role='editor',assigned_to=u,execution_number=n,is_current=False)
                S.objects.create(text=t,stage_type='editing',assignment=a,iteration=n,execution_number=n,is_current=False,is_completed=True,imported_completed=True)
        finally:importing_completed.reset(token)
        groups={x['label']:x['names'] for x in text_credit_groups(t)}
        names=groups['Redakcja']
        self.assertEqual(names.count('Zofia Alfa'),2)
        self.assertLess(names.index('Zofia Alfa'),names.index('Adam Zeta'))
        self.assertIn('Brak danych wykonawcy',names);self.assertNotIn('@',' '.join(names))

class MailBoundaryTests(SimpleTestCase):
    def raw(self,body):
        m=EmailMessage();m['Subject']='Nabór: „Test”';m.set_content(body)
        m.add_attachment(b'file',maintype='application',subtype='octet-stream',filename='test.docx')
        return m.as_bytes()
    def test_invalid_legacy_consent_not_silently_dropped(self):
        with self.assertRaises(MailboxError):parse_message(1,self.raw('Jan Autor;T;fantasy;100;jan@example.com;;Test;premierach, literowka'))
    def test_multiline_accepts_optional_boundary_and_footer_excluded(self):
        line='Jan Autor;T;fantasy;;100;jan@example.com;;premierach;Dziękuję.'
        without_marker=parse_message(1,self.raw(line+'\nDodatkowa treść'))
        self.assertEqual(parse_review_records(without_marker['record'])[0][0]['author_message'], 'Dziękuję.\nDodatkowa treść')
        result=parse_message(1,self.raw(line+'\nDrugi wiersz.\n--- KONIEC WIADOMOŚCI AUTORA ---\nIP: 127.0.0.1'))
        row=parse_review_records(result['record'])[0][0]
        self.assertEqual(row['author_message'],'Dziękuję.\nDrugi wiersz.')
        self.assertNotIn('127.0.0.1',row['author_message'])
        self.assertEqual(parse_review_records(parse_message(1,self.raw(line))['record'])[0][0]['author_message'],'Dziękuję.')
