"""Powiadomienia Discord: ścieżki błędów i konfiguracji dotąd nieobjęte testami – testowa wysyłka z panelu,
kolejka po zatwierdzeniu transakcji, konfiguracja webhooków i etykiety."""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.discord_notifications import (configured_channel_names, deliver, deliver_after_commit, rule_problems, safe_queue,
                                        workflow_changes)
from core.discord_webhook import DeliveryError, NoRedirect, send_message
from core.models import DiscordNotification, DiscordRole, NotificationRule
from core.notification_admin import NotificationRuleForm
from texts.models import Text
from workflow.models import WorkflowStage

URL = 'https://discord.com/api/webhooks/123/abc_DEF-1'


def configure(trigger, *, channel='Kanał', mention='', template='[etap]: [tytuł]'):
    rule = NotificationRule.objects.get(trigger=trigger)
    rule.enabled, rule.channel, rule.mention = True, channel, mention
    rule.save()
    rule.templates.all().delete()
    rule.templates.create(content=template)
    return rule


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example')
class TestPanelGapTests(TestCase):
    """Testowa wysyłka z panelu: błędy oznaczenia, kanału i dostarczenia trafiają do historii z właściwym statusem."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('admin', 'admin@example.com', 'test'))
        self.url = reverse('admin:core_notificationrule_test')

    def send(self, data=None, **patch_kwargs):
        with patch('core.discord_webhook.send_message', **patch_kwargs) as sent:
            response = self.client.post(self.url, {'title': 'Próba', 'trigger': 'stage_completed', **(data or {})})
        return response, sent

    def test_failures_are_reported_and_recorded(self):
        cases = {
            'kanał spoza konfiguracji serwera': ({'channel': 'Stary'}, {}, 'nie ma w konfiguracji', 'disabled'),
            'Discord odrzucił': ({}, {'side_effect': DeliveryError('Discord odrzucił żądanie.')},
                                 'Nie wysłano: Discord odrzucił', 'failed'),
            'brak potwierdzenia': ({}, {'side_effect': DeliveryError('Brak potwierdzenia.', uncertain=True)},
                                   'Nie wysłano: Brak potwierdzenia', 'unknown'),
        }
        for case, (rule_changes, effect, message, status) in cases.items():
            with self.subTest(case):
                configure('stage_completed', **rule_changes)
                response, _sent = self.send(**effect)
                self.assertContains(response, message)
                self.assertEqual(DiscordNotification.objects.latest('pk').status, status)

    def test_unknown_role_mention_is_refused_before_sending(self):
        DiscordRole.objects.create(name='usunięta', role_id='1')
        configure('stage_completed', mention='@usunięta')
        DiscordRole.objects.all().delete()
        response, sent = self.send()
        self.assertContains(response, 'Nie ma roli @usunięta')
        sent.assert_not_called()
        self.assertFalse(DiscordNotification.objects.exists())

    def test_test_mark_keeps_whole_link_within_discord_limit(self):
        configure('stage_completed', template='x' * 2100)
        response, sent = self.send({'mark_test': 'on'}, return_value='5')
        content = sent.call_args.args[1]
        self.assertEqual(len(content), 2000)
        self.assertTrue(content.startswith('[TEST] xxx'))
        self.assertTrue(content.endswith(f'(<https://cms.example{reverse("core:assigned_text_detail", args=[0])}>)'))
        self.assertContains(response, 'Wysłano na kanał')

    def test_panel_without_reminder_rule_or_some_announcement_rules(self):
        NotificationRule.objects.filter(trigger__in=('stage_waiting', 'review_new')).delete()
        page = self.client.get(self.url)
        self.assertEqual((page.context['waiting'], page.context['reminders']), (None, []))
        NotificationRule.objects.create(trigger='stage_waiting')
        page = self.client.get(self.url)
        self.assertNotIn('review_new', [row['trigger'] for row in page.context['reminders']])
        self.assertIn('first_proofreading', [row['trigger'] for row in page.context['reminders']])

    def test_rule_list_counts_templates_and_history_list_shortens_content(self):
        configure('stage_completed')
        DiscordNotification.objects.create(trigger='stage_completed', channel='Kanał', content='y' * 150)
        rules = self.client.get(reverse('admin:core_notificationrule_changelist'))
        row = next(rule for rule in rules.context['cl'].result_list if rule.trigger == 'stage_completed')
        self.assertEqual(rules.context['cl'].model_admin.template_count(row), 1)
        history = self.client.get(reverse('admin:core_discordnotification_changelist'))
        self.assertContains(history, '>' + 'y' * 100 + '<')
        self.assertNotContains(history, 'y' * 101)


class RuleFormChannelTests(TestCase):
    def test_rule_problems_name_broken_server_configuration(self):
        """Błędne DISCORD_WEBHOOKS: lista kanałów to None, a reguła dostaje jeden powód zamiast skargi na kanał."""
        rule = configure('first_proofreading')
        with override_settings(DISCORD_WEBHOOKS=['nie słownik']):
            configured = configured_channel_names()
        self.assertIsNone(configured)
        self.assertEqual(rule_problems(rule, configured), ['błędna konfiguracja kanałów na serwerze (DISCORD_WEBHOOKS)'])
        self.assertEqual(rule_problems(rule, {'Kanał'}), [])

    def test_channel_choices_follow_server_configuration(self):
        rule = NotificationRule.objects.get(trigger='first_proofreading')
        rule.channel = 'Dawny kanał'
        cases = {
            'poprawna konfiguracja i dawny kanał reguły': ({'Kanał': URL}, ['', 'Kanał', 'Dawny kanał'],
                                                           'Kanały z konfiguracji'),
            'błędna konfiguracja': ({'Kanał': 'http://example.com/hook'}, ['', 'Dawny kanał'],
                                    'nie skonfigurowano żadnego webhooka'),
        }
        for case, (webhooks, choices, help_text) in cases.items():
            with self.subTest(case), override_settings(DISCORD_WEBHOOKS=webhooks):
                field = NotificationRuleForm(instance=rule).fields['channel']
                self.assertEqual([value for value, _label in field.choices], choices)
                self.assertIn(help_text, field.help_text)


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL})
class QueueGapTests(TestCase):
    def notification(self, status='pending', channel='Kanał'):
        return DiscordNotification.objects.create(trigger='stage_completed', channel=channel, content='x', status=status)

    def test_queue_error_never_breaks_caller(self):
        configure('stage_completed')
        with patch('core.discord_notifications.queue', side_effect=RuntimeError('boom')), \
                self.assertLogs('core.discord_notifications', 'ERROR') as logs:
            self.assertIsNone(safe_queue('stage_completed', context={}))
        self.assertIn('Could not queue Discord notification stage_completed', logs.output[0])

    def test_delivery_outcomes_for_configuration_and_concurrent_runs(self):
        cases = {
            'wiadomość już wysyłana gdzie indziej': ('sending', {'DISCORD_WEBHOOKS': {'Kanał': URL}}, 'sending'),
            'błędna konfiguracja serwera': ('pending', {'DISCORD_WEBHOOKS': ['nie słownik']}, 'failed'),
        }
        for case, (status, settings, expected) in cases.items():
            with self.subTest(case), override_settings(**settings), \
                    patch('core.discord_webhook.send_message') as sent:
                item = self.notification(status)
                deliver(item.pk)
                item.refresh_from_db()
                self.assertEqual(item.status, expected)
                sent.assert_not_called()

    def test_failure_to_record_outcome_is_only_logged(self):
        item = self.notification()
        with patch('core.discord_notifications.deliver', side_effect=RuntimeError('boom')), \
                patch('core.models.DiscordNotification.objects.using', side_effect=RuntimeError('db')), \
                self.assertLogs('core.discord_notifications', 'ERROR') as logs:
            deliver_after_commit(item.pk)
        self.assertIn(f'Could not record notification outcome; notification={item.pk}', logs.output[-1])
        item.refresh_from_db()
        self.assertEqual(item.status, 'pending')

    def test_stage_moved_to_history_is_not_a_completion_for_non_work_stages(self):
        configure('stage_completed')
        text = Text.objects.create(title='Tekst', length=10)
        today = timezone.localdate()
        stage = WorkflowStage.objects.create(text=text, workflow_cycle=1, is_completed=True,
                                             started_at=today, ended_at=today,
                                             stage_type=WorkflowStage.StageType.READY_FOR_EDITING)
        before = {stage.pk: {'type': stage.stage_type, 'completed': False}}
        workflow_changes('default', text, before, {})
        self.assertFalse(DiscordNotification.objects.exists())


class WebhookAndLabelTests(TestCase):
    def test_redirect_or_unexpected_status_is_not_followed_and_is_uncertain(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://example.com/'))
        response = MagicMock(status=204)
        response.__enter__.return_value = response
        with patch('core.discord_webhook.build_opener') as opener, self.assertRaises(DeliveryError) as caught:
            opener.return_value.open.return_value = response
            send_message(URL, 'treść')
        self.assertTrue(caught.exception.uncertain)
        response.read.assert_not_called()

    def test_labels(self):
        role = DiscordRole.objects.create(name='korektor', role_id='5')
        item = DiscordNotification(trigger='stage_completed', channel='Kanał', content='x')
        self.assertEqual((str(role), str(item)), ('@korektor', 'Rozpoczęcie i zakończenie etapu (wszystkie etapy) → Kanał'))
