"""Powiadomienia Discord z reguł w panelu admina: treści z tagami, oznaczanie ról, zdarzenia obiegu i recenzji,
testowa wysyłka z panelu."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.discord_notifications import parse_mention, render
from core.discord_webhook import send_message
from core.models import DiscordNotification, DiscordRole, NotificationRule
from core.notification_models import validate_template_tags
from core.workflow_events import event_scope
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from texts.models import Anthology, Review
from workflow.models import WorkflowStage
from workflow.services import claim_stage, complete_stage, finish_editing_to_coordinator, resume_editing

StageType = WorkflowStage.StageType
URL = 'https://discord.com/api/webhooks/123/abc_DEF-1'
CONTEXT = {'tytuł': 'Latarnik', 'antologia': 'Cienie', 'gatunek': 'fantasy', 'długość': '25 000', 'etap': 'Redakcja'}


class RenderTests(SimpleTestCase):
    def test_tags_and_mention_position(self):
        cases = {
            'all tags': ('„[tytuł]” – [antologia], [gatunek], [długość] zzs, [etap]', '',
                         '„Latarnik” – Cienie, fantasy, 25 000 zzs, Redakcja'),
            'tags are case-insensitive': ('[Tytuł]', '', 'Latarnik'),
            'mention prepended': ('Nowy tekst: [tytuł]', '<@&42>', '<@&42> Nowy tekst: [tytuł]'.replace('[tytuł]', 'Latarnik')),
            'mention at [rola]': ('Hej [rola], „[tytuł]” czeka', '<@&42>', 'Hej <@&42>, „Latarnik” czeka'),
            'empty value': ('[gatunek]!', '', '–!'),
        }
        for case, (template, mention, expected) in cases.items():
            context = {**CONTEXT, 'gatunek': ''} if case == 'empty value' else CONTEXT
            with self.subTest(case):
                self.assertEqual(render(template, context, mention), expected)

    def test_message_is_cut_to_discord_limit(self):
        self.assertEqual(len(render('x' * 1500 + '[tytuł]' * 100, CONTEXT)), 2000)

    def test_unknown_tags_are_rejected(self):
        validate_template_tags('[tytuł] [antologia] [rola]')
        with self.assertRaisesMessage(ValidationError, '[autor]'):
            validate_template_tags('[tytuł] [autor]')

    def test_webhook_allows_only_chosen_mentions(self):
        cases = {'roles': ((['42'], False), {'parse': [], 'roles': ['42']}),
                 'here': (([], True), {'parse': ['everyone'], 'roles': []}),
                 'none': (([], False), {'parse': [], 'roles': []})}
        for case, ((roles, here), expected) in cases.items():
            with self.subTest(case), patch('core.discord_webhook.build_opener') as build:
                build.return_value.open.return_value.__enter__.return_value.status = 200
                build.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"id": "1"}'
                send_message(URL, 'x', role_ids=roles, here=here)
                payload = json.loads(build.return_value.open.call_args.args[0].data)
                self.assertEqual(payload['allowed_mentions'], expected)


class MentionTests(TestCase):
    def test_mentions(self):
        DiscordRole.objects.create(name='redaktor', role_id='123456')
        cases = {'@redaktor': (['123456'], False), '@Redaktor': (['123456'], False), '@here': ([], True), '': ([], False)}
        for raw, expected in cases.items():
            with self.subTest(raw):
                self.assertEqual(parse_mention(raw), expected)
        for raw in ('@everyone', 'redaktor', '@nieznana'):
            with self.subTest(raw), self.assertRaises(ValueError):
                parse_mention(raw)


def enable(trigger, template='[etap]: [tytuł]', mention=''):
    rule = NotificationRule.objects.get(trigger=trigger)
    rule.enabled, rule.channel, rule.mention = True, 'Kanał', mention
    rule.save()
    rule.templates.all().delete()
    rule.templates.create(content=template)
    return rule


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL})
class WorkflowTriggerTests(WorkflowTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        for trigger in NotificationRule.Trigger.values:
            enable(trigger)
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.sent = sender.start()
        self.addCleanup(sender.stop)

    def triggers(self):
        return list(DiscordNotification.objects.order_by('pk').values_list('trigger', 'content'))

    def test_default_rules_exist_and_are_disabled(self):
        NotificationRule.objects.update(enabled=False)
        self.assertEqual(NotificationRule.objects.count(), 8)
        with self.captureOnCommitCallbacks(execute=True):
            self.begin_editing()
        self.assertEqual(self.triggers(), [])

    def test_claiming_editing_announces_first_verification_and_completion(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.begin_editing()
        self.assertEqual(self.triggers(), [('first_verification', 'Pierwsza weryfikacja: Tekst do testów workflow')])
        self.assertEqual(DiscordNotification.objects.get().status, 'sent')

    def test_full_verification_path(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.finish_second_verification()
        triggers = [t for t, _ in self.triggers()]
        self.assertEqual(triggers.count('first_verification'), 1)  # rezerwacja nie ogłasza ponownie
        self.assertEqual(triggers.count('second_verification'), 1)
        completed = [c for t, c in self.triggers() if t == 'stage_completed']
        self.assertIn('Pierwsza weryfikacja: Tekst do testów workflow', completed)
        self.assertIn('Druga weryfikacja: Tekst do testów workflow', completed)

    def test_editing_control_opens_first_proofreading(self):
        self.finish_second_verification()
        resume_editing(self.text, self.editor, started_at=self.today)
        finish_editing_to_coordinator(self.text, self.editor, ended_at=self.today)
        control = claim_stage(self.text, StageType.EDITING_CONTROL, self.coordinator, self.today)
        DiscordNotification.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            complete_stage(control, self.coordinator, self.today, send_to_proofreading=True)
        self.assertEqual(sorted(t for t, _ in self.triggers()), ['first_proofreading', 'stage_completed'])

    def test_new_text_announces_editing_with_role_mention(self):
        DiscordRole.objects.create(name='redaktor', role_id='777')
        enable('ready_for_editing', 'Nowy tekst „[tytuł]” ([antologia], [długość] zzs)', '@redaktor')
        book = Anthology.objects.create(title='Cienie miasta')
        with self.captureOnCommitCallbacks(execute=True), event_scope(self.superuser):
            text = self.text.__class__.objects.create(title='Latarnik', length=25000, anthology=book)
            WorkflowStage.objects.create(text=text, workflow_cycle=1, stage_type=StageType.READY_FOR_EDITING)
        self.assertEqual(self.triggers(), [('ready_for_editing', '<@&777> Nowy tekst „Latarnik” (Cienie miasta, 25 000 zzs)')])
        self.assertEqual(self.sent.call_args.kwargs, {'role_ids': ['777'], 'here': False})

    def test_delivery_problem_never_breaks_workflow(self):
        self.sent.side_effect = RuntimeError('boom')
        with self.captureOnCommitCallbacks(execute=True):
            self.begin_editing()
        self.assertEqual(self.stage(StageType.EDITING).assignment.assigned_to, self.editor)
        self.assertEqual(DiscordNotification.objects.get().status, 'unknown')


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL})
class ReviewTriggerTests(TestCase):
    def setUp(self):
        enable('review_new', 'Do recenzji: [tytuł] ([gatunek])')
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.sent = sender.start()
        self.addCleanup(sender.stop)

    def review(self, i=0, **extra):
        book = Anthology.objects.get_or_create(title='Nabór')[0]
        return Review.objects.create(title=f'Zgłoszenie {i}', genre='horror', length=1000, email=f'a{i}@example.com',
                                     author_first_name='Jan', author_last_name=f'Nowak{i}', anthology=book, **extra)

    def test_new_review_is_announced_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            review = self.review()
        review.title = 'Zmiana'
        with self.captureOnCommitCallbacks(execute=True):
            review.save()
        self.assertEqual(list(DiscordNotification.objects.values_list('content', flat=True)), ['Do recenzji: Zgłoszenie 0 (horror)'])

    def test_bulk_import_is_capped(self):
        with self.captureOnCommitCallbacks(execute=True):
            for i in range(8):
                self.review(i)
        self.assertEqual(DiscordNotification.objects.count(), 5)

    def test_hidden_or_decided_reviews_are_skipped(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.review(1, is_hidden=True)
            self.review(2, status=Review.Status.REJECTED)
        self.assertFalse(DiscordNotification.objects.exists())


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL})
class AdminPanelTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'test')
        self.client.force_login(self.admin)
        DiscordRole.objects.create(name='korektor', role_id='555')

    def test_rule_form_validates_channel_and_mention(self):
        rule = NotificationRule.objects.get(trigger='first_proofreading')
        url = reverse('admin:core_notificationrule_change', args=[rule.pk])
        base = {'templates-TOTAL_FORMS': '0', 'templates-INITIAL_FORMS': '0', 'templates-MIN_NUM_FORMS': '0',
                'templates-MAX_NUM_FORMS': '1000'}
        cases = {
            'unknown role': ({'enabled': 'on', 'channel': 'Kanał', 'mention': '@nikt'}, 'Nie ma roli @nikt'),
            'no channel': ({'enabled': 'on', 'channel': '', 'mention': ''}, 'wymaga kanału'),
        }
        token = lambda: self.client.get(url).context['request'].edit_version_token  # noqa: E731
        for case, (data, error) in cases.items():
            with self.subTest(case):
                self.assertContains(self.client.post(url, {**base, **data, '_edit_version': token()}), error)
        response = self.client.post(url, {**base, 'enabled': 'on', 'channel': 'Kanał', 'mention': '@korektor',
                                          '_edit_version': token()})
        self.assertEqual(response.status_code, 302)
        rule.refresh_from_db()
        self.assertEqual((rule.enabled, rule.channel, rule.mention), (True, 'Kanał', '@korektor'))

    def test_test_panel_sends_each_trigger_immediately(self):
        rule = NotificationRule.objects.get(trigger='third_proofreading')
        rule.channel, rule.mention = 'Kanał', '@korektor'
        rule.save()
        rule.templates.all().delete()
        rule.templates.create(content='[etap]: „[tytuł]” ([antologia], [gatunek], [długość])')
        url = reverse('admin:core_notificationrule_test')
        self.assertContains(self.client.get(url), 'Nowy tekst do trzeciej korekty')
        data = {'title': 'Próba', 'anthology': 'Antologia X', 'genre': 'sf', 'length': '12000', 'stage': '',
                'mark_test': 'on', 'trigger': 'third_proofreading'}
        with patch('core.discord_webhook.send_message', return_value='9') as sent:
            response = self.client.post(url, data)
        self.assertContains(response, 'Wysłano na kanał')
        sent.assert_called_once_with(URL, '[TEST] <@&555> Trzecia korekta: „Próba” (Antologia X, sf, 12 000)',
                                     role_ids=['555'], here=False)
        self.assertEqual(DiscordNotification.objects.get().status, 'sent')

    def test_test_panel_explains_missing_setup(self):
        url = reverse('admin:core_notificationrule_test')
        rule = NotificationRule.objects.get(trigger='second_verification')
        rule.templates.all().delete()
        cases = {'no channel': ('', 'wybierz najpierw kanał'), 'no templates': ('Kanał', 'dodaj najpierw')}
        for case, (channel, error) in cases.items():
            rule.channel = channel
            rule.save()
            with self.subTest(case), patch('core.discord_webhook.send_message') as sent:
                response = self.client.post(url, {'title': 'X', 'trigger': 'second_verification'})
                self.assertContains(response, error)
                sent.assert_not_called()
