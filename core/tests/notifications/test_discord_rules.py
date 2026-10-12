"""Powiadomienia Discord z reguł w panelu admina: treści z tagami, oznaczanie ról, zdarzenia obiegu i recenzji,
testowa wysyłka z panelu."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.discord_notifications import parse_mention, queue, render
from core.discord_webhook import DeliveryError, send_message
from core.models import DiscordNotification, DiscordRole, NotificationRule
from core.notification_models import validate_template_tags
from core.workflow_events import event_scope
from core.services.texts import change_scheduled_stage, start_assigned_stage
from fantazmaty.testing import create_member
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

    def test_claiming_editing_announces_only_first_verification(self):
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

    def editing_control(self):
        self.finish_second_verification()
        resume_editing(self.text, self.editor, started_at=self.today)
        finish_editing_to_coordinator(self.text, self.editor, ended_at=self.today)
        control = claim_stage(self.text, StageType.EDITING_CONTROL, self.coordinator, self.today)
        DiscordNotification.objects.all().delete()
        return control

    def finish(self, stage_type, user):
        """Przejmuje etap z puli i od razu go kończy; zwraca nowe powiadomienia tej operacji."""
        DiscordNotification.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            stage = claim_stage(self.text, stage_type, user, self.today)
            complete_stage(stage, user, self.today)
        return sorted(self.triggers())

    def test_editing_control_opens_first_proofreading(self):
        control = self.editing_control()
        with self.captureOnCommitCallbacks(execute=True):
            complete_stage(control, self.coordinator, self.today, send_to_proofreading=True)
        self.assertEqual(sorted(t for t, _ in self.triggers()), ['first_proofreading', 'stage_completed'])

    def test_further_editing_reports_completed_control_once(self):
        # Kontrola trafia od razu do historii (nie jest już bieżąca) – zakończenie i tak jest ogłaszane.
        control = self.editing_control()
        with self.captureOnCommitCallbacks(execute=True):
            complete_stage(control, self.coordinator, self.today, send_to_proofreading=False)
        self.assertEqual(self.triggers(), [('stage_completed', 'Kontrola K. redakcji: Tekst do testów workflow')])

    def test_rest_of_workflow_announces_third_verification_and_third_proofreading(self):
        verifier_3 = create_member('weryfikator_3', 'Weryfikator')
        control = self.editing_control()
        complete_stage(control, self.coordinator, self.today, send_to_proofreading=True)
        title = 'Tekst do testów workflow'
        steps = [
            ('pierwsza korekta → druga korekta (tylko KK, bez ogłoszenia)', StageType.FIRST_PROOFREADING, self.proofreader,
             [('stage_completed', f'Pierwsza korekta: {title}')]),
            ('druga korekta → trzecia weryfikacja', StageType.SECOND_PROOFREADING, self.proofreading_coordinator,
             [('stage_completed', f'Druga korekta: {title}'), ('third_verification', f'Trzecia weryfikacja: {title}')]),
            ('trzecia weryfikacja → kontrola KW', StageType.THIRD_VERIFICATION, verifier_3,
             [('stage_completed', f'Trzecia weryfikacja: {title}')]),
            ('kontrola KW → kontrola redaktora', StageType.COORDINATOR_CONTROL, self.coordinator,
             [('stage_completed', f'Kontr. wer. (koordynator): {title}')]),
        ]
        for case, stage_type, user, expected in steps:
            with self.subTest(case):
                self.assertEqual(self.finish(stage_type, user), expected)
        DiscordNotification.objects.all().delete()
        editor_control = self.stage(StageType.EDITOR_CONTROL)
        with self.captureOnCommitCallbacks(execute=True):
            start_assigned_stage(user=self.editor, stage_id=editor_control.pk, started_at=self.today)
            complete_stage(self.stage(StageType.EDITOR_CONTROL), self.editor, self.today)
        self.assertEqual(sorted(self.triggers()), [('stage_completed', f'Kontr. wer. (redaktor): {title}'),
                                                   ('third_proofreading', f'Trzecia korekta: {title}')])

    def test_released_assignment_is_announced_again(self):
        control = self.editing_control()
        complete_stage(control, self.coordinator, self.today, send_to_proofreading=True)
        stage = claim_stage(self.text, StageType.FIRST_PROOFREADING, self.proofreader, self.today)
        DiscordNotification.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            change_scheduled_stage(user=self.proofreader, stage_id=stage.pk, cancel=True)
        self.assertEqual(self.triggers(), [('first_proofreading', 'Pierwsza korekta: Tekst do testów workflow')])

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


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL})
class QueueDeliveryTests(TestCase):
    def send(self, rule_channel='Kanał', mention='', effect=None):
        rule = enable('stage_completed', '[etap]: [tytuł]', mention)
        rule.channel = rule_channel
        rule.save()
        with patch('core.discord_webhook.send_message', return_value='77', side_effect=effect) as sent:
            with self.captureOnCommitCallbacks(execute=True):
                item = queue('stage_completed', context=CONTEXT)
        item.refresh_from_db()
        return item, sent

    def test_delivery_outcomes(self):
        cases = {
            'sent': ({}, 'sent', '77'),
            'channel missing from server config': ({'rule_channel': 'Inny'}, 'disabled', ''),
            'discord refused': ({'effect': DeliveryError('x')}, 'failed', ''),
            'no confirmation': ({'effect': DeliveryError('x', uncertain=True)}, 'unknown', ''),
        }
        for case, (kwargs, status, message_id) in cases.items():
            with self.subTest(case):
                item, sent = self.send(**kwargs)
                self.assertEqual((item.status, item.message_id), (status, message_id))
                self.assertEqual(sent.called, case != 'channel missing from server config')

    def test_mentions_in_queued_messages(self):
        DiscordRole.objects.create(name='stara', role_id='999')
        cases = {
            '@here': ('@here', '@here Redakcja: Latarnik', '', True),
            'role deleted after saving the rule': ('@stara', 'Redakcja: Latarnik', '', False),
        }
        for case, (mention, content, role_ids, here) in cases.items():
            if case == 'role deleted after saving the rule':
                DiscordRole.objects.filter(name='stara').delete()
            with self.subTest(case):
                item, sent = self.send(mention=mention)
                self.assertEqual((item.content, item.role_ids, item.mention_here), (content, role_ids, here))
                self.assertEqual(sent.call_args.kwargs, {'role_ids': [], 'here': here})

    def test_disabled_rule_or_rule_without_templates_queues_nothing(self):
        rule = enable('stage_completed')
        cases = {'disabled': {'enabled': False}, 'no channel': {'channel': ''}}
        for case, changes in cases.items():
            NotificationRule.objects.filter(pk=rule.pk).update(**{'enabled': True, 'channel': 'Kanał', **changes})
            with self.subTest(case):
                self.assertIsNone(queue('stage_completed', context=CONTEXT))
        NotificationRule.objects.filter(pk=rule.pk).update(enabled=True, channel='Kanał')
        rule.templates.all().delete()
        self.assertIsNone(queue('stage_completed', context=CONTEXT))


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL})
class AdminAccessTests(TestCase):
    def test_only_superuser_can_open_settings_and_send_tests(self):
        coordinator = create_member('koordynator', 'Koordynator')
        coordinator.is_staff = True
        coordinator.save()
        enable('stage_completed')
        urls = [reverse('admin:core_notificationrule_changelist'), reverse('admin:core_notificationrule_test'),
                reverse('admin:core_discordrole_changelist'), reverse('admin:core_discordnotification_changelist')]
        for label, login in (('anonymous', None), ('coordinator without superuser', coordinator)):
            if login:
                self.client.force_login(login)
            for url in urls:
                with self.subTest(label, url=url):
                    response = self.client.get(url)
                    self.assertIn(response.status_code, (302, 403))
                    self.assertNotContains(response, 'Wyślij test', status_code=response.status_code)
            with self.subTest(label, action='send test'), patch('core.discord_webhook.send_message') as sent:
                self.client.post(urls[1], {'title': 'X', 'trigger': 'stage_completed'})
                sent.assert_not_called()
        self.assertFalse(DiscordNotification.objects.exists())
