"""Powiadomienia Discord z reguł w panelu admina: treści z tagami, oznaczanie ról, zdarzenia obiegu i recenzji,
testowa wysyłka z panelu."""
import json
import re
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.discord_notifications import parse_mention, queue, render, send_waiting_reminders, text_authors, workflow_changes
from core.discord_webhook import DeliveryError, send_message
from core.models import DiscordNotification, DiscordRole, NotificationRule, WorkflowEvent
from core.notification_models import validate_template_tags
from core.workflow_events import event_scope
from core.services.reviews import assign_reviewer
from core.services.texts import change_scheduled_stage, start_assigned_stage
from fantazmaty.testing import create_member
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from texts.models import Anthology, ForeignAuthor, Review, Text, TextTranslation
from workflow.models import WorkflowStage
from workflow.services import (claim_stage, complete_stage, finish_editing_to_coordinator, resume_editing,
                               send_text_to_author, send_to_first_verification, send_to_second_verification,
                               start_first_verification)

StageType = WorkflowStage.StageType
URL = 'https://discord.com/api/webhooks/123/abc_DEF-1'
OLD_CHANNEL_URL = 'https://discord.com/api/webhooks/456/dawny-kanal'
LINK = '[O, tutaj](<https://cms.example/t/1/>)'
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

    def test_preview_link_position(self):
        cases = {
            'appended at the end': ('Nowy: [tytuł]', 'Nowy: Latarnik ' + LINK),
            'placed at [link]': ('[link] czeka „[tytuł]”', LINK + ' czeka „Latarnik”'),
        }
        for case, (template, expected) in cases.items():
            with self.subTest(case):
                self.assertEqual(render(template, CONTEXT, '', LINK), expected)

    def test_message_is_cut_to_discord_limit(self):
        long = 'x' * 1500 + '[tytuł]' * 100
        cases = {'no link': (long, ''), 'link appended': (long, LINK), 'link inside': ('[link]' + long, LINK)}
        for case, (template, link) in cases.items():
            with self.subTest(case):
                content = render(template, CONTEXT, '', link)
                self.assertEqual(len(content), 2000)
                self.assertTrue(content.endswith(link))  # odnośnik nigdy nie jest ucinany

    def test_unknown_tags_are_rejected(self):
        validate_template_tags('[tytuł] [autor] [antologia] [osoba] [rola] [link]')
        with self.assertRaisesMessage(ValidationError, '[nieznany]'):
            validate_template_tags('[tytuł] [nieznany]')

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
                self.assertNotIn('flags', payload)

    def test_webhook_can_suppress_link_previews(self):
        with patch('core.discord_webhook.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.status = 200
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"id": "1"}'
            send_message(URL, 'x', suppress_embeds=True)
            self.assertEqual(json.loads(build.return_value.open.call_args.args[0].data)['flags'], 4)


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


PREVIEW = re.compile(r' \[O, tutaj\]\(<[^>]+>\)$')


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example/')
class WorkflowTriggerTests(WorkflowTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        for trigger in NotificationRule.Trigger.values:
            enable(trigger)
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.sent = sender.start()
        self.addCleanup(sender.stop)

    def triggers(self):
        """(zdarzenie, treść) bez odnośnika do podglądu – ten sprawdza osobny test."""
        return [(trigger, PREVIEW.sub('', content))
                for trigger, content in DiscordNotification.objects.order_by('pk').values_list('trigger', 'content')]

    def test_default_rules_exist_and_are_disabled(self):
        NotificationRule.objects.update(enabled=False)
        self.assertEqual(NotificationRule.objects.count(), 9)
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
        self.assertEqual(triggers.count('first_verification'), 1)  # W1 zarezerwowana – koniec redakcji nie ogłasza jej ponownie
        self.assertEqual(triggers.count('second_verification'), 1)
        completed = [c for t, c in self.triggers() if t == 'stage_completed']
        self.assertIn('Pierwsza weryfikacja: Tekst do testów workflow', completed)
        self.assertIn('Druga weryfikacja: Tekst do testów workflow', completed)

    def test_editing_handoff_announces_first_verification_only_without_verifier(self):
        """Koniec pierwszego przebiegu redakcji bez zarezerwowanego W1 ponownie ogłasza „do wzięcia” (raz,
        z odnośnikiem); z zarezerwowanym W1 albo przy powtórzonym W1 (kolejne podejście) – nie."""
        DiscordRole.objects.create(name='weryfikator', role_id='555')
        enable('first_verification', 'Do wzięcia: [tytuł]', '@weryfikator')
        title = 'Tekst do testów workflow'
        for case, reserved, iteration, expected in (
            ('bez weryfikatora', False, 1, [('first_verification', f'<@&555> Do wzięcia: {title}')]),
            ('z weryfikatorem', True, 1, []),
            ('powtórzone W1', False, 2, []),
        ):
            with self.subTest(case):
                self.text = self.text.__class__.objects.create(title=title, length=25000)
                WorkflowStage.objects.create(text=self.text, workflow_cycle=1, stage_type=StageType.READY_FOR_EDITING)
                link = f'[O, tutaj](<https://cms.example{reverse("core:assigned_text_detail", args=[self.text.pk])}>)'
                self.begin_editing()
                if reserved:
                    claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
                WorkflowStage.objects.filter(pk=self.stage(StageType.FIRST_VERIFICATION).pk).update(iteration=iteration)
                DiscordNotification.objects.all().delete()
                with self.captureOnCommitCallbacks(execute=True):
                    send_to_first_verification(self.text, self.editor, ended_at=self.today)
                self.assertEqual(sorted(self.triggers()), expected)
                for item in DiscordNotification.objects.filter(trigger='first_verification'):
                    self.assertTrue(item.content.endswith(' ' + link))
                    self.assertEqual(item.stage_id, self.stage(StageType.FIRST_VERIFICATION).pk)

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
        self.assertEqual(self.sent.call_args.kwargs, {'role_ids': ['777'], 'here': False, 'suppress_embeds': True})
        link = f'[O, tutaj](<https://cms.example{reverse("core:assigned_text_detail", args=[text.pk])}>)'
        self.assertTrue(DiscordNotification.objects.get().content.endswith(' ' + link))

    def test_delivery_problem_never_breaks_workflow(self):
        self.sent.side_effect = RuntimeError('boom')
        with self.captureOnCommitCallbacks(execute=True):
            self.begin_editing()
        self.assertEqual(self.stage(StageType.EDITING).assignment.assigned_to, self.editor)
        self.assertEqual(DiscordNotification.objects.get().status, 'unknown')

    @override_settings(DISCORD_WEBHOOKS={'Kanał': URL, 'Testowy': OLD_CHANNEL_URL})
    def test_workflow_events_are_logged_but_only_rules_reach_discord(self):
        """Operacja workflow zapisuje WorkflowEvent, ale na Discord idą wyłącznie wiadomości z reguł – dawny kanał
        „Testowy” nie dostaje nic; przy wyłączonych regułach nie ma żadnej wysyłki."""
        NotificationRule.objects.update(enabled=False)
        with self.captureOnCommitCallbacks(execute=True):
            self.begin_editing()
        self.assertTrue(WorkflowEvent.objects.filter(text=self.text, kind=WorkflowEvent.Kind.WORKFLOW).exists())
        self.sent.assert_not_called()
        NotificationRule.objects.update(enabled=True)
        events = WorkflowEvent.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
            send_to_first_verification(self.text, self.editor, ended_at=self.today)
            stage = start_first_verification(self.text, self.verifier_1, started_at=self.today)
            complete_stage(stage, self.verifier_1, self.today)
        self.assertGreater(WorkflowEvent.objects.count(), events)
        self.assertEqual(self.sent.call_count, DiscordNotification.objects.count())
        self.assertGreater(self.sent.call_count, 0)
        self.assertEqual({call.args[0] for call in self.sent.call_args_list}, {URL})


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example')
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
        link = f'[O, tutaj](<https://cms.example{reverse("core:assigned_review_detail", args=[review.pk])}>)'
        self.assertEqual(list(DiscordNotification.objects.values_list('content', flat=True)),
                         [f'Do recenzji: Zgłoszenie 0 (horror) {link}'])

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

    def test_rolled_back_review_does_not_leak_into_the_next_transaction(self):
        """ID zgłoszenia z wycofanej transakcji nie zostaje w partii do ogłoszenia: ukryte zgłoszenie, które po
        wycofaniu dostaje ten sam pk, nie jest ogłaszane – ogłoszone jest tylko jawne z kolejnej transakcji."""
        with transaction.atomic():
            rolled_back = self.review(0)
            transaction.set_rollback(True)
        with self.captureOnCommitCallbacks(execute=True):
            hidden = self.review(1, is_hidden=True)
            fresh = self.review(2)
        if hidden.pk != rolled_back.pk:
            self.skipTest('Baza nie użyła ponownie klucza z wycofanej transakcji – scenariusz wycieku niemożliwy.')
        self.assertEqual(list(DiscordNotification.objects.values_list('review_id', flat=True)), [fresh.pk])

    def test_batches_are_kept_per_connection_not_in_module_state(self):
        """Lista zgłoszeń do ogłoszenia żyje na połączeniu tylko do zatwierdzenia transakcji."""
        from django.db import connection
        from core.discord_notifications import _PENDING_ATTR
        with self.captureOnCommitCallbacks(execute=True):
            self.review(3)
            self.assertEqual(getattr(connection, _PENDING_ATTR).ids, [Review.objects.get(title='Zgłoszenie 3').pk])
        self.assertFalse(hasattr(connection, _PENDING_ATTR))
        self.assertEqual(DiscordNotification.objects.count(), 1)


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example')
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
        # Testowego tekstu nie ma w bazie – odnośnik prowadzi do nieistniejącego podglądu nr 0.
        link = f'[O, tutaj](<https://cms.example{reverse("core:assigned_text_detail", args=[0])}>)'
        sent.assert_called_once_with(URL, f'[TEST] <@&555> Trzecia korekta: „Próba” (Antologia X, sf, 12 000) {link}',
                                     role_ids=['555'], here=False, suppress_embeds=True)
        self.assertEqual(DiscordNotification.objects.get().status, 'sent')

    def test_test_panel_fills_author_and_person(self):
        """„Wyślij test”: [autor] i [osoba] z formularza; przy zgłoszeniu do recenzji autor zawsze „(anonimowo)”."""
        for trigger in ('stage_completed', 'review_new'):
            enable(trigger, '[autor] / [osoba]: [tytuł]')
        url = reverse('admin:core_notificationrule_test')
        self.assertContains(self.client.get(url), 'Anna Nowak, A.N. Kowal')
        cases = {'stage_completed': 'Ewa Lis, Mrok / Jan Kowalski: Próba', 'review_new': '(anonimowo) / –: Próba'}
        for trigger, expected in cases.items():
            data = {'title': 'Próba', 'author': 'Ewa Lis, Mrok', 'person': 'Jan Kowalski', 'trigger': trigger}
            with self.subTest(trigger), patch('core.discord_webhook.send_message', return_value='9') as sent:
                self.client.post(url, data)
                self.assertTrue(sent.call_args.args[1].startswith(expected))

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
                self.assertEqual(sent.call_args.kwargs, {'role_ids': [], 'here': here, 'suppress_embeds': True})

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
class ResendCommandTests(TestCase):
    """Komenda resend_discord_notifications: które stany i w jakim wieku są ponawiane."""

    def notification(self, status, age):
        item = DiscordNotification.objects.create(trigger='stage_completed', channel='Kanał', content='x', status=status)
        DiscordNotification.objects.filter(pk=item.pk).update(created_at=timezone.now() - age)
        return item

    def test_which_notifications_are_retried(self):
        cases = {
            'fresh pending is left to the application': ('pending', timedelta(minutes=5), 'pending'),
            'old pending is sent': ('pending', timedelta(minutes=11), 'sent'),
            'failed is sent': ('failed', timedelta(hours=1), 'sent'),
            'unknown is never repeated': ('unknown', timedelta(hours=1), 'unknown'),
            'failed older than 3 days stays failed': ('failed', timedelta(days=3, minutes=1), 'failed'),
            'sent is untouched': ('sent', timedelta(minutes=11), 'sent'),
        }
        items = {case: self.notification(status, age) for case, (status, age, _) in cases.items()}
        out = StringIO()
        with patch('core.discord_webhook.send_message', return_value='55') as sent:
            call_command('resend_discord_notifications', stdout=out)
        self.assertEqual(sent.call_count, 2)
        for case, (_, _, expected) in cases.items():
            with self.subTest(case):
                items[case].refresh_from_db()
                self.assertEqual(items[case].status, expected)
        self.assertEqual(out.getvalue().strip(), 'Ponowione powiadomienia: 2 (Wysłano: 2)')

    def test_failed_retry_is_reported_and_stays_failed(self):
        item = self.notification('failed', timedelta(hours=1))
        out = StringIO()
        with patch('core.discord_webhook.send_message', side_effect=DeliveryError('x')):
            call_command('resend_discord_notifications', stdout=out)
        item.refresh_from_db()
        self.assertEqual(item.status, 'failed')
        self.assertEqual(out.getvalue().strip(), 'Ponowione powiadomienia: 1 (Błąd wysyłki: 1)')
        with patch('core.discord_webhook.send_message', return_value='56') as sent:
            call_command('resend_discord_notifications', stdout=StringIO())
        self.assertTrue(sent.called)
        item.refresh_from_db()
        self.assertEqual((item.status, item.message_id), ('sent', '56'))


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


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL, 'Przypomnienia': URL}, CMS_BASE_URL='https://cms.example')
class WaitingReminderTests(WorkflowTestDataMixin, TestCase):
    """Przypomnienia po 7, 14 i 21 dniach od ogłoszenia, że etap jest do wzięcia."""

    def setUp(self):
        super().setUp()
        enable('first_verification', 'Do wzięcia: [tytuł]', '@here')
        self.rule = enable('stage_waiting', '[dni] dni – [etap]: [tytuł]')
        NotificationRule.objects.filter(pk=self.rule.pk).update(channel='', mention='')
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.sent = sender.start()
        self.addCleanup(sender.stop)
        with self.captureOnCommitCallbacks(execute=True):
            self.begin_editing()  # ogłasza pierwszą weryfikację
        self.announcement = DiscordNotification.objects.get(trigger='first_verification')

    def run_after(self, days, announcement=None):
        start = (announcement or self.announcement).created_at
        with self.captureOnCommitCallbacks(execute=True):
            return send_waiting_reminders(now=start + timedelta(days=days))

    def reminders(self):
        return list(DiscordNotification.objects.filter(trigger='stage_waiting').order_by('pk')
                    .values_list('reminder_days', flat=True))

    def test_reminders_after_7_14_and_21_days_each_once(self):
        runs = [(1, []), (6.9, []), (7, [7]), (8, [7]), (13, [7]), (14, [7, 14]), (14.5, [7, 14]),
                (21, [7, 14, 21]), (27, [7, 14, 21])]
        for days, expected in runs:
            with self.subTest(day=days):
                self.run_after(days)
                self.assertEqual(self.reminders(), expected)

    def test_reminder_content_channel_and_mention_come_from_first_announcement(self):
        self.run_after(7)
        item = DiscordNotification.objects.get(trigger='stage_waiting')
        link = f'[O, tutaj](<https://cms.example{reverse("core:assigned_text_detail", args=[self.text.pk])}>)'
        self.assertEqual(item.content, f'@here 7 dni – Pierwsza weryfikacja: Tekst do testów workflow {link}')
        self.assertEqual((item.channel, item.mention_here, item.reminder_of, item.stage_id, item.status),
                         ('Kanał', True, self.announcement, self.announcement.stage_id, 'sent'))
        self.assertEqual(self.sent.call_args.kwargs, {'role_ids': [], 'here': True, 'suppress_embeds': True})

    def test_own_channel_and_mention_override_first_announcement(self):
        DiscordRole.objects.create(name='weryfikator', role_id='321')
        NotificationRule.objects.filter(pk=self.rule.pk).update(channel='Przypomnienia', mention='@weryfikator')
        self.run_after(7)
        item = DiscordNotification.objects.get(trigger='stage_waiting')
        self.assertEqual((item.channel, item.role_ids, item.mention_here), ('Przypomnienia', '321', False))
        self.assertTrue(item.content.startswith('<@&321> 7 dni'))

    def test_after_a_break_only_the_latest_overdue_reminder_is_sent(self):
        cases = {'job resumed on day 15': (15, [14]), 'job resumed on day 25': (25, [21]),
                 'past the reminder window': (29, [])}
        for case, (days, expected) in cases.items():
            DiscordNotification.objects.filter(trigger='stage_waiting').delete()
            with self.subTest(case):
                self.run_after(days)
                self.assertEqual(self.reminders(), expected)

    def test_no_reminder_when_someone_took_the_stage_or_reminders_are_off(self):
        cases = {
            'stage claimed': lambda: claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1),
            'reminder rule disabled': lambda: NotificationRule.objects.filter(pk=self.rule.pk).update(enabled=False),
            'reminder rule without templates': lambda: self.rule.templates.all().delete(),
        }
        for case, change in cases.items():
            with self.subTest(case):
                sid = transaction.savepoint()
                change()
                self.assertEqual(self.run_after(7), 0)
                self.assertEqual(self.reminders(), [])
                transaction.savepoint_rollback(sid)

    def test_released_stage_restarts_the_count_from_the_new_announcement(self):
        stage = claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
        DiscordNotification.objects.filter(pk=self.announcement.pk).update(
            created_at=self.announcement.created_at - timedelta(days=10))
        with self.captureOnCommitCallbacks(execute=True):
            change_scheduled_stage(user=self.verifier_1, stage_id=stage.pk, cancel=True)
        again = DiscordNotification.objects.filter(trigger='first_verification').latest('pk')
        self.assertNotEqual(again, self.announcement)
        self.assertEqual(self.run_after(3, again), 0)  # 13 dni od pierwszego, ale 3 od ponownego ogłoszenia
        self.run_after(7, again)
        self.assertEqual(list(DiscordNotification.objects.filter(trigger='stage_waiting')
                              .values_list('reminder_of', 'reminder_days')), [(again.pk, 7)])

    def test_daily_command(self):
        DiscordNotification.objects.filter(pk=self.announcement.pk).update(
            created_at=self.announcement.created_at - timedelta(days=7))
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command('send_discord_reminders', stdout=out)
        self.assertIn('Zakolejkowane przypomnienia: 1', out.getvalue())
        self.assertEqual(self.reminders(), [7])


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example')
class WaitingReminderAdminTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('admin', 'admin@example.com', 'test'))
        self.rule = NotificationRule.objects.get(trigger='stage_waiting')

    def test_default_reminder_rule_is_disabled_with_valid_templates(self):
        templates = list(self.rule.templates.values_list('content', flat=True))
        self.assertFalse(self.rule.enabled)
        self.assertEqual(len(templates), 3)
        for template in templates:
            with self.subTest(template=template):
                validate_template_tags(template)
                self.assertIn('[dni]', template)

    def test_reminder_rule_can_be_enabled_without_own_channel(self):
        url = reverse('admin:core_notificationrule_change', args=[self.rule.pk])
        page = self.client.get(url)
        self.assertContains(page, 'ten sam co w pierwszym powiadomieniu')
        response = self.client.post(url, {'templates-TOTAL_FORMS': '0', 'templates-INITIAL_FORMS': '0',
                                          'templates-MIN_NUM_FORMS': '0', 'templates-MAX_NUM_FORMS': '1000',
                                          'enabled': 'on', 'channel': '', 'mention': '',
                                          '_edit_version': page.context['request'].edit_version_token})
        self.assertEqual(response.status_code, 302)
        self.rule.refresh_from_db()
        self.assertEqual((self.rule.enabled, self.rule.channel), (True, ''))

    def test_reminder_rule_page_lists_where_reminders_go(self):
        NotificationRule.objects.filter(trigger='first_proofreading').update(channel='Kanał', mention='@korektor')
        page = self.client.get(reverse('admin:core_notificationrule_change', args=[self.rule.pk]))
        self.assertContains(page, 'po „Nowy tekst do pierwszej korekty” → kanał: Kanał, oznaczenie: @korektor')
        self.assertContains(page, 'po „Nowy tekst do recenzji” → kanał: – brak (przypomnienie nie wyjdzie) –')

    def test_test_panel_has_reminder_button_for_each_announcement(self):
        page = self.client.get(reverse('admin:core_notificationrule_test'))
        for value in ('review_new', 'ready_for_editing', 'first_proofreading', 'third_proofreading',
                      'first_verification', 'second_verification', 'third_verification'):
            with self.subTest(value):
                self.assertContains(page, f'name="reminder_for" value="{value}"')
        self.assertNotContains(page, 'name="trigger" value="stage_waiting"')

    def test_test_panel_sends_reminder_like_the_first_announcement(self):
        DiscordRole.objects.create(name='korektor', role_id='555')
        NotificationRule.objects.filter(trigger='first_proofreading').update(channel='Kanał', mention='@korektor')
        NotificationRule.objects.filter(trigger='review_new').update(channel='Kanał', mention='@here')
        self.rule.templates.all().delete()
        self.rule.templates.create(content='[etap] czeka [dni] dni: [tytuł]')
        text_link = f'[O, tutaj](<https://cms.example{reverse("core:assigned_text_detail", args=[0])}>)'
        review_link = f'[O, tutaj](<https://cms.example{reverse("core:assigned_review_detail", args=[0])}>)'
        cases = {
            'first proofreading': ('first_proofreading', f'<@&555> Pierwsza korekta czeka 14 dni: Próba {text_link}',
                                   ['555'], False, 'przypomnienie po: „Nowy tekst do pierwszej korekty”'),
            'review': ('review_new', f'@here Recenzja czeka 14 dni: Próba {review_link}', [], True,
                       'przypomnienie po: „Nowy tekst do recenzji”'),
        }
        for case, (base, content, roles, here, note) in cases.items():
            data = {'title': 'Próba', 'days': '14', 'reminder_for': base}
            with self.subTest(case), patch('core.discord_webhook.send_message', return_value='9') as sent:
                response = self.client.post(reverse('admin:core_notificationrule_test'), data)
                self.assertContains(response, note)
                sent.assert_called_once_with(URL, content, role_ids=roles, here=here, suppress_embeds=True)

    def test_test_panel_rejects_unknown_reminder(self):
        with patch('core.discord_webhook.send_message') as sent:
            for data in ({'title': 'X', 'reminder_for': 'stage_completed'}, {'title': 'X', 'trigger': 'stage_waiting'}):
                with self.subTest(**data):
                    self.assertContains(self.client.post(reverse('admin:core_notificationrule_test'), data),
                                        'Nieznany rodzaj wiadomości')
        sent.assert_not_called()


@override_settings(DISCORD_WEBHOOKS={'Recenzje': URL}, CMS_BASE_URL='https://cms.example')
class ReviewReminderTests(TestCase):
    """Zgłoszenie do recenzji, którego nikt nie wziął, też dostaje przypomnienia."""

    def setUp(self):
        rule = enable('review_new', 'Do recenzji: [tytuł]', '@here')
        NotificationRule.objects.filter(pk=rule.pk).update(channel='Recenzje')
        waiting = enable('stage_waiting', '[dni] dni – [etap]: [tytuł]')
        NotificationRule.objects.filter(pk=waiting.pk).update(channel='', mention='')
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.sent = sender.start()
        self.addCleanup(sender.stop)
        with self.captureOnCommitCallbacks(execute=True):
            self.review = Review.objects.create(
                title='Zgłoszenie', genre='horror', length=1000, email='a@example.com', author_first_name='Jan',
                author_last_name='Nowak', anthology=Anthology.objects.create(title='Nabór'))
        self.announcement = DiscordNotification.objects.get(trigger='review_new')

    def run_after(self, days):
        with self.captureOnCommitCallbacks(execute=True):
            return send_waiting_reminders(now=self.announcement.created_at + timedelta(days=days))

    def test_unclaimed_review_gets_reminders_on_its_channel(self):
        for days in (6, 7, 14, 21):
            self.run_after(days)
        items = DiscordNotification.objects.filter(trigger='stage_waiting').order_by('pk')
        self.assertEqual([i.reminder_days for i in items], [7, 14, 21])
        link = f'[O, tutaj](<https://cms.example{reverse("core:assigned_review_detail", args=[self.review.pk])}>)'
        self.assertEqual((items[0].content, items[0].channel, items[0].review, items[0].status),
                         (f'@here 7 dni – Recenzja: Zgłoszenie {link}', 'Recenzje', self.review, 'sent'))

    def test_no_reminder_for_taken_hidden_or_decided_review(self):
        cases = {
            'reviewer assigned': lambda: assign_reviewer(user=create_member('recenzent', 'Recenzent'), review_id=self.review.pk),
            'hidden': lambda: Review.objects.filter(pk=self.review.pk).update(is_hidden=True),
            'rejected': lambda: Review.objects.filter(pk=self.review.pk).update(status=Review.Status.REJECTED),
        }
        for case, change in cases.items():
            with self.subTest(case):
                sid = transaction.savepoint()
                change()
                self.assertEqual(self.run_after(7), 0)
                transaction.savepoint_rollback(sid)


def author(first, last, pseudonym=''):
    return Author.objects.create(first_name=first, last_name=last, pseudonym=pseudonym, email=f'{first}.{last}@example.com')


class AuthorTagTests(TestCase):
    """[autor]: podpis jak w stopce tekstu – pseudonim albo imię i nazwisko, kilku autorów po przecinku."""

    def test_author_signature(self):
        cases = {
            'brak autora': ([], ''),
            'imię i nazwisko': ([('Anna', 'Nowak', '')], 'Anna Nowak'),
            'pseudonim zamiast nazwiska': ([('Jan', 'Kowalski', 'J.K. Mrok')], 'J.K. Mrok'),
            'kilku autorów w kolejności podpisu': ([('Zofia', 'Zięba', ''), ('Adam', 'Abacki', 'Ada')], 'Ada, Zofia Zięba'),
        }
        for case, (people, expected) in cases.items():
            with self.subTest(case):
                text = Text.objects.create(title=case, length=10)
                text.authors.set([author(*person) for person in people])
                self.assertEqual(text_authors(text), expected)
                self.assertEqual(render('„[tytuł]” – [autor]', {'tytuł': 'T', 'autor': text_authors(text)}),
                                 f'„T” – {expected or "–"}')

    def test_translation_shows_foreign_authors(self):
        book = Anthology.objects.create(title='Przekłady', is_translated=True)
        text = Text.objects.create(title='Przekład', length=10, anthology=book)
        TextTranslation.objects.get_or_create(text=text)[0].foreign_authors.set([
            ForeignAuthor.objects.create(first_name='Ursula', last_name='Le Guin')])
        self.assertEqual(text_authors(text), 'Ursula Le Guin')


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example')
class AuthorInNotificationsTests(WorkflowTestDataMixin, TestCase):
    """[autor] jest podstawiany we wszystkich wiadomościach o tekstach; przy zgłoszeniach do recenzji
    (anonimowych dla recenzentów) zawsze „(anonimowo)”."""

    def setUp(self):
        super().setUp()
        for trigger in NotificationRule.Trigger.values:
            enable(trigger, '[autor] | [tytuł]')
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.addCleanup(sender.stop)
        sender.start()

    def test_every_text_and_review_notification(self):
        self.text.authors.set([author('Anna', 'Nowak'), author('Jan', 'Kowalski', 'Mrok')])
        with self.captureOnCommitCallbacks(execute=True), event_scope(self.superuser):
            text = Text.objects.create(title='Nowy', length=10)
            text.authors.set([author('Ewa', 'Lis')])
            WorkflowStage.objects.create(text=text, workflow_cycle=1, stage_type=StageType.READY_FOR_EDITING)
        with self.captureOnCommitCallbacks(execute=True):
            self.begin_editing()  # ogłasza pierwszą weryfikację
            send_to_first_verification(self.text, self.editor, ended_at=self.today)  # przekazanie, bez „zakończono”
            Review.objects.create(title='Zgłoszenie', genre='horror', length=1000, email='a@example.com',
                                  author_first_name='Jan', author_last_name='Nowak',
                                  anthology=Anthology.objects.create(title='Nabór'))
        start = DiscordNotification.objects.order_by('pk').first().created_at
        with self.captureOnCommitCallbacks(execute=True):
            send_waiting_reminders(now=start + timedelta(days=7, minutes=1))
        rows = DiscordNotification.objects.values_list('trigger', 'content')
        expected = {('ready_for_editing', 'Ewa Lis'), ('first_verification', 'Mrok, Anna Nowak'),
                    ('review_new', '(anonimowo)'),
                    ('stage_waiting', 'Mrok, Anna Nowak'), ('stage_waiting', '(anonimowo)'),
                    ('stage_waiting', 'Ewa Lis')}
        self.assertEqual({(trigger, content.split(' | ')[0]) for trigger, content in rows}, expected)


class StageCompletedDefaultTests(TestCase):
    def test_single_disabled_rule_without_channel_with_author_in_default_text(self):
        """Migracja zostawia jedną wyłączoną regułę bez kanału; domyślna treść ma [autor] i [osoba]."""
        rule = NotificationRule.objects.get(trigger='stage_completed')
        self.assertEqual((rule.enabled, rule.channel, rule.mention), (False, '', ''))
        self.assertEqual(list(rule.templates.values_list('content', flat=True)),
                         ['Zakończono etap „[etap]”: „[tytuł]” – [autor] ([antologia]). Wykonanie: [osoba].'])


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example')
class StageCompletedTests(WorkflowTestDataMixin, TestCase):
    """Jedna reguła „Zakończenie etapu” dla wszystkich etapów; bez wiadomości przy przekazaniu pliku autorowi
    i jego powrocie."""

    NOT_A_COMPLETION = {StageType.READY_FOR_EDITING, StageType.READY, StageType.WITHDRAWN, StageType.AUTHOR_EDITING}

    def setUp(self):
        super().setUp()
        self.rule = enable('stage_completed', '[etap] – [osoba]: [tytuł] – [autor]')
        self.text.authors.set([author('Anna', 'Nowak')])
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.addCleanup(sender.stop)
        sender.start()

    def completed(self):
        return list(DiscordNotification.objects.filter(trigger='stage_completed').order_by('pk')
                    .values_list('content', flat=True))

    def test_every_stage_type_gives_exactly_one_message(self):
        """Tabela po wszystkich StageType: zakończenie etapu → jedna wiadomość z [etap] i [osoba]
        (wykonawca etapu; bez wykonawcy – osoba, która wykonała operację). Redakcja – tylko gdy ta sama
        operacja przekazuje tekst koordynatorowi (otwiera Kontrolę K. redakcji)."""
        for stage_type, label in StageType.choices:
            for performer in ('Ewa Wykonawczyni', ''):
                with self.subTest(stage_type, performer=performer):
                    DiscordNotification.objects.all().delete()
                    stage = {'type': stage_type, 'type_display': label, 'released': True, 'started': True,
                             'assigned': bool(performer), 'iteration': 1, 'performer': performer}
                    after = {1: {**stage, 'completed': True}}
                    if stage_type == StageType.EDITING:
                        after[2] = {'type': 'editing_control', 'type_display': 'Kontrola K. redakcji',
                                    'released': False, 'started': False, 'assigned': True, 'completed': False}
                    with self.captureOnCommitCallbacks(execute=True):
                        workflow_changes('default', self.text, {1: {**stage, 'completed': False}},
                                         after, actor='Koordynator Główny')
                    who = performer or 'Koordynator Główny'
                    expected = ([] if stage_type in self.NOT_A_COMPLETION
                                else [f'{label} – {who}: Tekst do testów workflow – Anna Nowak'])
                    self.assertEqual([PREVIEW.sub('', c) for c in self.completed()], expected)

    def test_author_round_trip_reports_only_real_end_of_editing(self):
        """Redaktor wysyła plik autorowi, autor go oddaje, przekazuje tekst do W2 – bez wiadomości o zakończeniu
        redakcji; dopiero przekazanie koordynatorowi (ostatnie) – dokładnie jedna wiadomość."""
        self.finish_first_verification()
        resume_editing(self.text, self.editor, started_at=self.today)
        DiscordNotification.objects.all().delete()
        steps = [
            ('wysłanie do autora', lambda: send_text_to_author(self.text, self.editor, started_at=self.today), []),
            ('powrót od autora', lambda: resume_editing(self.text, self.editor, started_at=self.today), []),
            ('przekazanie do W2', lambda: send_to_second_verification(self.text, self.editor, started_at=self.today),
             []),
        ]
        for case, action, expected in steps:
            with self.subTest(case):
                DiscordNotification.objects.all().delete()
                with self.captureOnCommitCallbacks(execute=True):
                    action()
                self.assertEqual([PREVIEW.sub('', c) for c in self.completed()], expected)
        w2 = claim_stage(self.text, StageType.SECOND_VERIFICATION, self.verifier_2, self.today)
        complete_stage(w2, self.verifier_2, self.today)
        resume_editing(self.text, self.editor, started_at=self.today)
        DiscordNotification.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            finish_editing_to_coordinator(self.text, self.editor, ended_at=self.today)
        self.assertEqual([PREVIEW.sub('', c) for c in self.completed()],
                         ['Redakcja – redaktor Testowy: Tekst do testów workflow – Anna Nowak'])

    def test_disabled_rule_sends_nothing(self):
        NotificationRule.objects.filter(pk=self.rule.pk).update(enabled=False)
        with self.captureOnCommitCallbacks(execute=True):
            self.finish_first_verification()
        self.assertEqual(self.completed(), [])
