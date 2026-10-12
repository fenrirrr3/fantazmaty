"""Panel admina: trwałe usunięcie tekstu razem z etapami i całą historią – i niczym więcej."""
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from authors.models import Author
from core.audiobook_models import Audiobook, AudiobookStage
from core.edit_versions import version_of
from core.models import DiscordNotification, WorkflowEvent
from core.workflow_events import event_scope
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from illustrations.models import Illustration
from texts.models import Anthology, Review, Text, TextNote
from workflow.models import WorkflowHandoff, WorkflowRepetition, WorkflowRoleAssignment, WorkflowStage


class TextPurgeTests(WorkflowTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.anthology = Anthology.objects.create(title='Cienie miasta')
        self.author = Author.objects.create(first_name='Jan', last_name='Nowak')
        self.text.anthology = self.anthology
        self.text.save()
        self.text.authors.add(self.author)
        with event_scope(self.superuser):
            self.finish_second_verification()
        TextNote.objects.create(text=self.text, author=self.editor, content='Uwaga')
        stage = AudiobookStage.objects.create(text=self.text, stage_type='proofreading', position=1)
        Audiobook.objects.create(text=self.text, active_stage=stage)
        DiscordNotification.objects.create(trigger='stage_completed', channel='Kanał', content='x', text=self.text)
        LogEntry.objects.log_action(self.superuser.pk, ContentType.objects.get_for_model(Text).pk, self.text.pk,
                                    self.text.title, CHANGE, 'zmiana')
        self.review = Review.objects.create(title='Zgłoszenie', length=1000, email='a@example.com', author_first_name='Jan',
                                            author_last_name='Nowak', anthology=self.anthology, copied_text=self.text)
        self.other = Text.objects.create(title='Inny tekst', length=10, anthology=self.anthology)
        WorkflowStage.objects.create(text=self.other, workflow_cycle=1, stage_type=WorkflowStage.StageType.READY_FOR_EDITING)
        self.url = reverse('admin:texts_text_purge', args=[self.text.pk])
        self.client.force_login(self.superuser)

    def text_records(self, text_id):
        return {
            'stages': WorkflowStage.objects.filter(text_id=text_id).count(),
            'assignments': WorkflowRoleAssignment.objects.filter(text_id=text_id).count(),
            'handoffs': WorkflowHandoff.objects.filter(text_id=text_id).count(),
            'repetitions': WorkflowRepetition.objects.filter(text_id=text_id).count(),
            'notes': TextNote.objects.filter(text_id=text_id).count(),
            'audiobook': Audiobook.objects.filter(text_id=text_id).count(),
            'audiobook stages': AudiobookStage.objects.filter(text_id=text_id).count(),
            'events': WorkflowEvent.objects.filter(text_id=text_id).count(),
            'discord': DiscordNotification.objects.filter(text_id=text_id).count(),
            'admin log': LogEntry.objects.filter(object_id=str(text_id)).count(),
        }

    def post(self, **data):
        return self.client.post(self.url, {'version': version_of(self.text), **data})

    def test_change_page_links_to_purge(self):
        page = self.client.get(reverse('admin:texts_text_change', args=[self.text.pk]))
        self.assertContains(page, self.url)

    def test_confirmation_page_lists_what_will_be_deleted(self):
        page = self.client.get(self.url)
        for line in ('etapy:', 'przydziały osób:', 'notatki: 1', 'audiobook: 1', 'historia zmian workflow:',
                     'wysłane powiadomienia Discord: 1', 'wpisy dziennika panelu admina: 1'):
            with self.subTest(line):
                self.assertContains(page, line)
        self.assertTrue(Text.objects.filter(pk=self.text.pk).exists())

    def test_purge_deletes_text_with_whole_history_and_nothing_else(self):
        before = self.text_records(self.text.pk)
        self.assertTrue(all(before[key] for key in ('stages', 'assignments', 'events', 'audiobook', 'admin log')))
        other_before = self.text_records(self.other.pk)
        events_before = WorkflowEvent.objects.exclude(text=self.text).count()
        response = self.post(confirm='on')
        self.assertRedirects(response, reverse('admin:texts_text_changelist'))
        self.assertFalse(Text.objects.filter(pk=self.text.pk).exists())
        self.assertEqual(self.text_records(self.text.pk), dict.fromkeys(before, 0))
        # Usunięcie nie zostawia nowego wpisu „Usunięty tekst” w historii ani wiadomości na Discordzie.
        self.assertEqual(WorkflowEvent.objects.count(), events_before)
        self.assertFalse(DiscordNotification.objects.exists())
        self.assertEqual(self.text_records(self.other.pk), other_before)
        self.review.refresh_from_db()
        self.assertEqual((self.review.copied_text_id, self.review.publication_detached), (None, True))
        for kept in (self.anthology, self.author, self.editor, self.verifier_1, self.verifier_2):
            with self.subTest(kept=str(kept)):
                self.assertTrue(type(kept).objects.filter(pk=kept.pk).exists())

    def test_nothing_is_deleted_without_confirmation_or_with_stale_version(self):
        cases = {'no confirmation': ({}, 'To pole jest wymagane'),
                 'stale version': ({'confirm': 'on', 'version': version_of(self.text) - 1}, 'zmienił się w międzyczasie')}
        for case, (data, error) in cases.items():
            with self.subTest(case):
                self.assertContains(self.post(**data), error)
                self.assertTrue(Text.objects.filter(pk=self.text.pk).exists())

    def test_illustrations_block_purge(self):
        Illustration.objects.create(text=self.text)
        self.assertContains(self.client.get(self.url), 'przypisane ilustracje')
        self.post(confirm='on')
        self.assertTrue(Text.objects.filter(pk=self.text.pk).exists())

    def test_only_superuser(self):
        self.client.force_login(self.coordinator)
        self.assertIn(self.post(confirm='on').status_code, (302, 403))
        self.assertTrue(Text.objects.filter(pk=self.text.pk).exists())
