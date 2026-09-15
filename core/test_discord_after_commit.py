from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings

from core.discord_webhook import DeliveryError
from core.models import WorkflowEvent
from core.workflow_events import event_scope
from texts.models import Text
from workflow.models import WorkflowStage


@override_settings(DISCORD_WEBHOOKS={'Testowy': 'https://discord.com/api/webhooks/1/test'})
class AfterCommitNotificationTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='actor')
        self.text = Text.objects.create(title='Commit test', length=1000)

    def change(self):
        with event_scope(self.user):
            return WorkflowStage.objects.create(text=self.text, stage_type='editing')

    @patch('core.discord_webhook.send_message')
    def test_nested_transactions_send_only_after_outer_commit(self, send):
        def acknowledged(*args):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(WorkflowStage.objects.filter(text=self.text).exists())
            return '123'
        send.side_effect = acknowledged
        with transaction.atomic():
            with transaction.atomic():
                self.change()
            send.assert_not_called()
        send.assert_called_once()
        self.assertEqual(WorkflowEvent.objects.get().status, 'sent')

    @patch('core.discord_webhook.send_message')
    def test_outer_rollback_sends_nothing(self, send):
        with self.assertRaises(ValueError):
            with transaction.atomic():
                self.change()
                raise ValueError('rollback')
        send.assert_not_called()
        self.assertFalse(WorkflowEvent.objects.exists())
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())

    @patch('core.discord_webhook.send_message', side_effect=DeliveryError('timeout', uncertain=True))
    def test_transport_failure_preserves_committed_stage(self, send):
        with transaction.atomic():
            stage = self.change()
        self.assertTrue(WorkflowStage.objects.filter(pk=stage.pk).exists())
        self.assertEqual(WorkflowEvent.objects.get().status, 'unknown')
        send.assert_called_once()

    @patch('core.workflow_events.deliver', side_effect=RuntimeError('private transport detail'))
    def test_unexpected_failure_cannot_escape_after_commit(self, deliver):
        with self.assertLogs('core.workflow_events', level='ERROR') as logs:
            with transaction.atomic():
                stage = self.change()
        self.assertTrue(WorkflowStage.objects.filter(pk=stage.pk).exists())
        self.assertEqual(WorkflowEvent.objects.get().status, 'unknown')
        self.assertNotIn('private transport detail', str(logs.output))
