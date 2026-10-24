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
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.edit_versions import version_of
from core.discord_notifications import (audiobook_changed, parse_mention, queue, render, send_waiting_reminders,
                                        text_authors, workflow_changes)
from core.discord_webhook import DeliveryError, send_message
from core.models import DiscordNotification, DiscordRole, NotificationRule, WorkflowEvent
from core.notification_models import validate_template_tags
from core.workflow_events import event_scope
from core.services.reviews import assign_reviewer
from core.services.texts import change_scheduled_stage, start_assigned_stage
from core.workflow_tokens import make_token
from core.tests.anthologies.support import AnthologyPageData, admin_form_data
from fantazmaty.testing import create_member, edit_token
from fantazmaty.testing.workflow import WorkflowTestDataMixin
from texts.models import Anthology, ForeignAuthor, Review, Text, TextTranslation
from texts.novels import edit_token as novel_edit_token
from workflow.models import WorkflowStage
from workflow.services import (claim_stage, complete_stage, current_stage_queryset, finish_editing_to_coordinator,
                               resume_editing, send_text_to_author, send_to_first_verification,
                               send_to_second_verification, start_first_verification)

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
        enable('stage_completed', '[zdarzenie] [etap]: [tytuł]')
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.sent = sender.start()
        self.addCleanup(sender.stop)

    def triggers(self):
        """(zdarzenie, treść) bez odnośnika do podglądu – ten sprawdza osobny test."""
        return [(trigger, PREVIEW.sub('', content))
                for trigger, content in DiscordNotification.objects.order_by('pk').values_list('trigger', 'content')]

    def test_default_rules_exist_and_are_disabled(self):
        NotificationRule.objects.update(enabled=False)
        self.assertEqual(set(NotificationRule.objects.values_list('trigger', flat=True)),
                         set(NotificationRule.Trigger.values))
        with self.captureOnCommitCallbacks(execute=True):
            self.begin_editing()
        self.assertEqual(self.triggers(), [])

    def test_claiming_editing_announces_first_verification_and_editing_start(self):
        """Przejęcie tekstu do redakcji: ogłoszenie W1 do wzięcia (rezerwacja) i „Rozpoczęto Redakcja”."""
        with self.captureOnCommitCallbacks(execute=True):
            self.begin_editing()
        self.assertEqual(sorted(self.triggers()), [('first_verification', 'Pierwsza weryfikacja: Tekst do testów workflow'),
                                                   ('stage_completed', 'Rozpoczęto Redakcja: Tekst do testów workflow')])
        self.assertEqual(set(DiscordNotification.objects.values_list('status', flat=True)), {'sent'})

    def test_full_verification_path(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.finish_second_verification()
        triggers = [t for t, _ in self.triggers()]
        self.assertEqual(triggers.count('first_verification'), 1)  # W1 zarezerwowana – koniec redakcji nie ogłasza jej ponownie
        self.assertEqual(triggers.count('second_verification'), 1)
        completed = [c for t, c in self.triggers() if t == 'stage_completed']
        self.assertIn('Zakończono Pierwsza weryfikacja: Tekst do testów workflow', completed)
        self.assertIn('Zakończono Druga weryfikacja: Tekst do testów workflow', completed)

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
                self.assertEqual(sorted(t for t in self.triggers() if t[0] == 'first_verification'), expected)
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
        self.assertEqual(self.triggers(), [('stage_completed', 'Zakończono Kontrola K. redakcji: Tekst do testów workflow')])

    def test_rest_of_workflow_announces_third_verification_and_third_proofreading(self):
        verifier_3 = create_member('weryfikator_3', 'Weryfikator')
        control = self.editing_control()
        complete_stage(control, self.coordinator, self.today, send_to_proofreading=True)
        title = 'Tekst do testów workflow'
        steps = [
            ('pierwsza korekta → druga korekta (tylko KK, bez ogłoszenia)', StageType.FIRST_PROOFREADING, self.proofreader,
             [], 'Pierwsza korekta'),
            ('druga korekta → trzecia weryfikacja', StageType.SECOND_PROOFREADING, self.proofreading_coordinator,
             [('third_verification', f'Trzecia weryfikacja: {title}')], 'Druga korekta'),
            ('trzecia weryfikacja → kontrola KW', StageType.THIRD_VERIFICATION, verifier_3, [], 'Trzecia weryfikacja'),
            ('kontrola KW → kontrola redaktora', StageType.COORDINATOR_CONTROL, self.coordinator, [],
             'Kontr. wer. (koordynator)'),
        ]
        for case, stage_type, user, announced, label in steps:
            with self.subTest(case):
                # Przejęcie z datą rozpoczyna etap (osobna operacja), zakończenie – kolejna.
                self.assertEqual(self.finish(stage_type, user), sorted([
                    *announced, ('stage_completed', f'Rozpoczęto {label}: {title}'),
                    ('stage_completed', f'Zakończono {label}: {title}')]))
        DiscordNotification.objects.all().delete()
        editor_control = self.stage(StageType.EDITOR_CONTROL)
        with self.captureOnCommitCallbacks(execute=True):
            start_assigned_stage(user=self.editor, stage_id=editor_control.pk, started_at=self.today)
            complete_stage(self.stage(StageType.EDITOR_CONTROL), self.editor, self.today)
        self.assertEqual(sorted(self.triggers()), [('stage_completed', f'Rozpoczęto Kontr. wer. (redaktor): {title}'),
                                                   ('stage_completed', f'Zakończono Kontr. wer. (redaktor): {title}'),
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
        self.assertEqual(set(DiscordNotification.objects.values_list('status', flat=True)), {'unknown'})

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

    def test_test_panel_fills_author_person_and_audiobook_tags(self):
        """„Wyślij test”: [autor], [osoba], [zdarzenie], [zmiana] i [status] z formularza; przy zgłoszeniu do recenzji
        autor zawsze „(anonimowo)”, [zdarzenie] tylko przy „Rozpoczęcie i zakończenie etapu”, [zmiana] i [status] tylko
        przy „Produkcja audiobooka”, której odnośnik prowadzi do audiobooka."""
        for trigger in ('stage_completed', 'review_new', 'audiobook_production'):
            enable(trigger, '[autor] / [osoba] / [zdarzenie] / [zmiana] / [status]: [tytuł]')
        url = reverse('admin:core_notificationrule_test')
        page = self.client.get(url)
        self.assertContains(page, 'Anna Nowak, A.N. Kowal')
        self.assertContains(page, 'Produkcja audiobooka (wszystkie zmiany)')
        audio_link = reverse('core:audiobook_detail', args=[0])
        cases = {'stage_completed': ('Ewa Lis, Mrok / Jan Kowalski / Rozpoczęto / – / –: Próba', 'tekst'),
                 'review_new': ('(anonimowo) / – / – / – / –: Próba', 'recenzja'),
                 'audiobook_production': ('Ewa Lis, Mrok / Jan Kowalski / – / Zmieniono status: Montaż. / Montaż: Próba',
                                          audio_link)}
        for trigger, (expected, link) in cases.items():
            data = {'title': 'Próba', 'author': 'Ewa Lis, Mrok', 'person': 'Jan Kowalski', 'trigger': trigger,
                    'event': 'Rozpoczęto',
                    'change': 'Zmieniono status: Montaż.', 'audio_status': 'Montaż'}
            with self.subTest(trigger), patch('core.discord_webhook.send_message', return_value='9') as sent:
                self.client.post(url, data)
                self.assertTrue(sent.call_args.args[1].startswith(expected), sent.call_args.args[1])
                self.assertEqual(audio_link in sent.call_args.args[1], link == audio_link)

    def test_rule_page_explains_collective_rules(self):
        """Strona reguły zbiorczej („Rozpoczęcie i zakończenie etapu”, „Produkcja audiobooka”) wyjaśnia, kiedy
        wiadomość wychodzi."""
        cases = {'stage_completed': 'Jedna wiadomość po rozpoczęciu i po zakończeniu każdego etapu',
                 'audiobook_production': 'Jedna wiadomość po każdej zmianie w produkcji audiobooka'}
        for trigger, help_text in cases.items():
            rule = NotificationRule.objects.get(trigger=trigger)
            with self.subTest(trigger):
                self.assertContains(self.client.get(reverse('admin:core_notificationrule_change', args=[rule.pk])),
                                    help_text)

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

    def test_list_and_test_panel_warn_why_rule_will_not_send(self):
        """Lista reguł i panel testowy pokazują, dlaczego zwykłe zdarzenie nie wyśle wiadomości: reguła wyłączona
        (test wysyła mimo to), kanał spoza DISCORD_WEBHOOKS, brak treści, nieznana rola; poprawna – „działa”."""
        rule = NotificationRule.objects.get(trigger='stage_completed')
        cases = {
            'wyłączona': ({'enabled': False}, ['WYŁĄCZONE – test z panelu wysyła mimo to, ale zwykłe zdarzenia nie']),
            'kanał spoza konfiguracji': ({'channel': 'Dawny'}, ['kanału „Dawny” nie ma w konfiguracji serwera']),
            'bez treści': ({'templates': False}, ['brak treści wiadomości']),
            'nieznana rola': ({'mention': '@nikt'}, ['oznaczenie @nikt zostanie pominięte: Nie ma roli @nikt']),
            'działa': ({}, []),
        }
        lists = (reverse('admin:core_notificationrule_changelist'), reverse('admin:core_notificationrule_test'))
        for case, (change, expected) in cases.items():
            with self.subTest(case):
                enable('stage_completed')
                NotificationRule.objects.filter(pk=rule.pk).update(
                    enabled=change.get('enabled', True), channel=change.get('channel', 'Kanał'),
                    mention=change.get('mention', ''))
                if change.get('templates') is False:
                    rule.templates.all().delete()
                for url in lists:
                    page = self.client.get(url).content.decode()
                    for text in expected or ['działa']:
                        self.assertIn(text, page, url)

    def test_test_from_disabled_rule_is_sent_with_warning(self):
        """„Wyślij test” z wyłączonej reguły wysyła wiadomość, ale komunikat ostrzega, że zwykłe zdarzenia nie wyjdą."""
        for enabled, warned in ((False, True), (True, False)):
            enable('stage_completed')
            NotificationRule.objects.filter(trigger='stage_completed').update(enabled=enabled)
            with self.subTest(enabled=enabled), patch('core.discord_webhook.send_message', return_value='9'):
                response = self.client.post(reverse('admin:core_notificationrule_test'),
                                            {'title': 'X', 'trigger': 'stage_completed'})
                self.assertContains(response, 'Wysłano na kanał')
                self.assertEqual('Uwaga: to powiadomienie jest WYŁĄCZONE' in response.content.decode(), warned)


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
            'ten sam pseudonim dwóch autorów raz': ([('Jan', 'Kowal', 'Mrok'), ('Jan', 'Nowak', 'Mrok')], 'Mrok'),
        }
        self.assertEqual(text_authors(Text(title='Niezapisany', length=1)), '')
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
            send_to_first_verification(self.text, self.editor, ended_at=self.today)
            Review.objects.create(title='Zgłoszenie', genre='horror', length=1000, email='a@example.com',
                                  author_first_name='Jan', author_last_name='Nowak',
                                  anthology=Anthology.objects.create(title='Nabór'))
        start = DiscordNotification.objects.order_by('pk').first().created_at
        with self.captureOnCommitCallbacks(execute=True):
            send_waiting_reminders(now=start + timedelta(days=7, minutes=1))
        rows = DiscordNotification.objects.values_list('trigger', 'content')
        expected = {('ready_for_editing', 'Ewa Lis'), ('first_verification', 'Mrok, Anna Nowak'),
                    ('stage_completed', 'Rozpoczęto: Mrok, Anna Nowak'), ('stage_completed', 'Zakończono: Mrok, Anna Nowak'),
                    ('review_new', '(anonimowo)'),
                    ('stage_waiting', 'Mrok, Anna Nowak'), ('stage_waiting', '(anonimowo)'),
                    ('stage_waiting', 'Ewa Lis')}
        self.assertEqual({(trigger, content.split(' | ')[0]) for trigger, content in rows}, expected)


class StageCompletedDefaultTests(TestCase):
    def test_single_disabled_rule_without_channel_with_author_in_default_text(self):
        """Migracje zostawiają jedną wyłączoną regułę bez kanału o nazwie „Rozpoczęcie i zakończenie etapu”;
        domyślna treść ma [zdarzenie], [autor] i [osoba]."""
        rule = NotificationRule.objects.get(trigger='stage_completed')
        self.assertEqual((rule.enabled, rule.channel, rule.mention), (False, '', ''))
        self.assertEqual(str(rule), 'Rozpoczęcie i zakończenie etapu (wszystkie etapy)')
        self.assertEqual(list(rule.templates.values_list('content', flat=True)),
                         ['[zdarzenie] etap „[etap]”: „[tytuł]” – [autor] ([antologia]). Wykonanie: [osoba].'])

    def test_migration_0064_adds_event_tag_only_to_stage_rule(self):
        """Migracja 0064 daje to samo co wysyłka (with_event_tag): zmienia tylko pierwsze słowo zdarzenia i tylko
        w regule etapów; całe słowa – „zakończeniu” (inny przypadek) nie jest zdarzeniem; [Zdarzenie] wielką literą
        zostaje; treść innej reguły z „Zakończono” bez zmian; ponowne uruchomienie niczego nie zmienia."""
        from importlib import import_module
        from types import SimpleNamespace

        from django.apps import apps
        from django.db import connection

        from core.discord_notifications import with_event_tag
        migration = import_module('core.migrations.0064_discord_stage_event_tag')
        stage_rule = NotificationRule.objects.get(trigger='stage_completed')
        stage_rule.templates.all().delete()
        cases = {
            'ZAKOŃCZONO etap [etap]; rozpoczęcie kolejnego wkrótce': '[zdarzenie] etap [etap]; rozpoczęcie kolejnego wkrótce',
            'Po zakończeniu etapu [etap] – [osoba]': '[zdarzenie]: Po zakończeniu etapu [etap] – [osoba]',
            'Etap [etap] rozpoczęty': 'Etap [etap] [zdarzenie]',
            '[Zdarzenie] [etap]': '[Zdarzenie] [etap]',
        }
        for content in cases:
            stage_rule.templates.create(content=content)
        audiobook = NotificationRule.objects.get(trigger='audiobook_production').templates.create(
            content='Zakończono montaż: [tytuł]')
        editor = SimpleNamespace(connection=connection)
        for run in (1, 2):
            with self.subTest(run=run):
                migration.forwards(apps, editor)
                self.assertEqual(dict(zip(cases, stage_rule.templates.order_by('pk').values_list('content', flat=True))),
                                 cases)
                self.assertEqual({content: with_event_tag(content) for content in cases}, cases)
                audiobook.refresh_from_db()
                self.assertEqual(audiobook.content, 'Zakończono montaż: [tytuł]')


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example')
class StageCompletedTests(WorkflowTestDataMixin, TestCase):
    """Jedna reguła „Rozpoczęcie i zakończenie etapu” dla wszystkich etapów; każde przekazanie redakcji z celem
    w [etap], bez wiadomości przy przekazaniu pliku autorowi i jego powrocie."""

    NOT_A_COMPLETION = {StageType.READY_FOR_EDITING, StageType.READY, StageType.WITHDRAWN, StageType.AUTHOR_EDITING}

    def setUp(self):
        super().setUp()
        self.rule = enable('stage_completed', '[zdarzenie] [etap] – [osoba]: [tytuł] – [autor]')
        self.text.authors.set([author('Anna', 'Nowak')])
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.addCleanup(sender.stop)
        sender.start()

    def completed(self):
        return list(DiscordNotification.objects.filter(trigger='stage_completed').order_by('pk')
                    .values_list('content', flat=True))

    def test_every_stage_type_gives_exactly_one_message(self):
        """Tabela po wszystkich StageType: zakończenie etapu → jedna wiadomość z [etap] i [osoba]
        (wykonawca etapu; bez wykonawcy – osoba, która wykonała operację). Redakcja z celem przekazania
        (tu: koordynator – Kontrola K. redakcji)."""
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
                    shown = 'Redakcja → Kontrola K. redakcji' if stage_type == StageType.EDITING else label
                    expected = ([] if stage_type in self.NOT_A_COMPLETION
                                else [f'Zakończono {shown} – {who}: Tekst do testów workflow – Anna Nowak'])
                    self.assertEqual([PREVIEW.sub('', c) for c in self.completed()], expected)

    def test_editing_handover_label_names_target(self):
        """[etap] zakończonej redakcji wskazuje etap, do którego trafił tekst; przy wysłaniu pliku autorowi – brak
        wiadomości; bez rozpoznanego celu – sama „Redakcja”."""
        editing = {'type': 'editing', 'type_display': 'Redakcja', 'released': True, 'started': True,
                   'assigned': True, 'iteration': 1, 'performer': 'Ewa', 'completed': False}
        reserved_w1 = {'type': 'first_verification', 'type_display': 'Pierwsza weryfikacja', 'released': False,
                       'started': False, 'assigned': True, 'iteration': 1, 'completed': False}

        def opened(kind, label):
            return {'type': kind, 'type_display': label, 'released': True, 'started': False, 'assigned': False,
                    'iteration': 1, 'completed': False}
        for case, before_extra, after_extra, expected in (
            ('W1 (rezerwacja przekazana do pracy)', {2: reserved_w1}, {2: {**reserved_w1, 'released': True}},
             ['Zakończono Redakcja → Pierwsza weryfikacja – Ewa']),
            ('K1 (Ekstrakty)', {}, {3: opened('first_proofreading', 'Pierwsza korekta')},
             ['Zakończono Redakcja → Pierwsza korekta – Ewa']),
            ('autor', {}, {3: opened('author_editing', 'Plik u autora')}, []),
            ('bez celu', {}, {}, ['Zakończono Redakcja – Ewa']),
        ):
            with self.subTest(case):
                DiscordNotification.objects.all().delete()
                self.rule.templates.update(content='[zdarzenie] [etap] – [osoba]')
                with self.captureOnCommitCallbacks(execute=True):
                    workflow_changes('default', self.text, {1: editing, **before_extra},
                                     {1: {**editing, 'completed': True}, **after_extra}, actor='Koordynator')
                self.assertEqual([PREVIEW.sub('', c) for c in self.completed()], expected)

    def test_author_round_trip_reports_only_real_end_of_editing(self):
        """Wysłanie pliku autorowi i jego powrót – bez wiadomości; przekazanie do W2 i do koordynatora – po jednej
        wiadomości z celem przekazania."""
        self.finish_first_verification()
        resume_editing(self.text, self.editor, started_at=self.today)
        DiscordNotification.objects.all().delete()
        steps = [
            ('wysłanie do autora', lambda: send_text_to_author(self.text, self.editor, started_at=self.today), []),
            ('powrót od autora', lambda: resume_editing(self.text, self.editor, started_at=self.today), []),
            ('przekazanie do W2', lambda: send_to_second_verification(self.text, self.editor, started_at=self.today),
             ['Zakończono Redakcja → Druga weryfikacja – redaktor Testowy: Tekst do testów workflow – Anna Nowak']),
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
                         ['Zakończono Redakcja → Kontrola K. redakcji – redaktor Testowy: Tekst do testów workflow – '
                          'Anna Nowak'])

    def test_stage_starts(self):
        """Rozpoczęcie etapu: „Rozpoczęto [etap]” z wykonawcą (bez wykonawcy – osoba wykonująca operację), także
        dla etapu nowego w tej operacji i wznowienia redakcji po W1. Bez wiadomości: sama rezerwacja W1, Gotowe
        i praca autora, wznowienie redakcji po powrocie pliku od autora; etap rozpoczęty i zakończony w jednej
        operacji – tylko zakończenie."""
        def stage(kind, label, *, started=False, completed=False, performer='Ewa'):
            return {'type': kind, 'type_display': label, 'released': True, 'started': started, 'completed': completed,
                    'assigned': bool(performer), 'iteration': 1, 'performer': performer}
        k1 = ('first_proofreading', 'Pierwsza korekta')
        for case, before, after, expected in (
            ('przejęcie z rozpoczęciem', {1: stage(*k1, performer='')}, {1: stage(*k1, started=True)},
             ['Rozpoczęto Pierwsza korekta – Ewa']),
            ('rozpoczęcie bez wykonawcy', {1: stage(*k1, performer='')}, {1: stage(*k1, started=True, performer='')},
             ['Rozpoczęto Pierwsza korekta – Koordynator']),
            ('przejęcie tekstu do redakcji (nowy etap)', {1: stage('ready_for_editing', 'Do redakcji', performer='')},
             {2: stage('editing', 'Redakcja', started=True)}, ['Rozpoczęto Redakcja – Ewa']),
            ('start i zakończenie w jednej operacji', {1: stage(*k1)}, {1: stage(*k1, started=True, completed=True)},
             ['Zakończono Pierwsza korekta – Ewa']),
            ('rezerwacja W1', {1: stage('first_verification', 'Pierwsza weryfikacja', performer='')},
             {1: stage('first_verification', 'Pierwsza weryfikacja')}, []),
            ('Gotowe', {}, {1: stage('ready', 'Gotowe', started=True)}, []),
            ('wysłanie do autora', {}, {1: stage('author_editing', 'Plik u autora', started=True)}, []),
            ('powrót od autora', {1: stage('author_editing', 'Plik u autora', started=True)},
             {1: stage('author_editing', 'Plik u autora', started=True, completed=True),
              2: stage('editing', 'Redakcja', started=True)}, []),
            ('wznowienie redakcji po W1',
             {1: stage('first_verification', 'Pierwsza weryfikacja', started=True, completed=True)},
             {1: stage('first_verification', 'Pierwsza weryfikacja', started=True, completed=True),
              2: stage('editing', 'Redakcja', started=True)}, ['Rozpoczęto Redakcja – Ewa']),
        ):
            with self.subTest(case):
                DiscordNotification.objects.all().delete()
                self.rule.templates.update(content='[zdarzenie] [etap] – [osoba]')
                with self.captureOnCommitCallbacks(execute=True):
                    workflow_changes('default', self.text, before, after, actor='Koordynator')
                self.assertEqual([PREVIEW.sub('', c) for c in self.completed()], expected)

    def test_template_without_event_tag_still_distinguishes_start(self):
        """Treść bez [zdarzenie] (np. dawna „Zakończono etap…” z panelu): rozpoczęcie nie jest ogłaszane jako
        zakończenie – tag zastępuje pierwsze słowo „Zakończono/Rozpoczęto…”, a bez niego trafia na początek."""
        stage = {'type': 'first_proofreading', 'type_display': 'Pierwsza korekta', 'released': True, 'assigned': True,
                 'iteration': 1, 'performer': 'Ewa', 'started': False, 'completed': False}
        for template, started, completed in (
            ('Zakończono etap [etap] – [osoba]', 'Rozpoczęto etap Pierwsza korekta – Ewa',
             'Zakończono etap Pierwsza korekta – Ewa'),
            ('Etap zakończony: [etap]', 'Etap Rozpoczęto: Pierwsza korekta', 'Etap Zakończono: Pierwsza korekta'),
            ('[etap] – [osoba]', 'Rozpoczęto: Pierwsza korekta – Ewa', 'Zakończono: Pierwsza korekta – Ewa'),
        ):
            for event, after, expected in (('start', {**stage, 'started': True}, started),
                                           ('koniec', {**stage, 'started': True, 'completed': True}, completed)):
                with self.subTest(template=template, event=event):
                    DiscordNotification.objects.all().delete()
                    self.rule.templates.update(content=template)
                    before = {1: stage if event == 'start' else {**stage, 'started': True}}
                    with self.captureOnCommitCallbacks(execute=True):
                        workflow_changes('default', self.text, before, {1: after}, actor='Koordynator')
                    self.assertEqual([PREVIEW.sub('', c) for c in self.completed()], [expected])

    def test_disabled_rule_sends_nothing(self):
        NotificationRule.objects.filter(pk=self.rule.pk).update(enabled=False)
        with self.captureOnCommitCallbacks(execute=True):
            self.finish_first_verification()
        self.assertEqual(self.completed(), [])


@tag('slow')
@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example')
class StageCompletedHttpTests(WorkflowTestDataMixin, TestCase):
    """Jak na produkcji: włączona reguła „Rozpoczęcie i zakończenie etapu” z kanałem i treścią, operacje wyłącznie
    przez żądania HTTP (karta tekstu, „Teksty do wzięcia”, admin). Każdy krok porównuje wiadomości o rozpoczęciu
    i zakończeniu z ogłoszeniem następnego etapu w tym samym żądaniu."""

    def setUp(self):
        super().setUp()
        for trigger in NotificationRule.Trigger.values:
            enable(trigger, '[etap]')
        enable('stage_completed', '[zdarzenie] [etap]')
        self.text.anthology = Anthology.objects.create(title='Antologia testowa')
        self.text.save()
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.sent = sender.start()
        self.addCleanup(sender.stop)
        self.day = self.today.isoformat()

    def post(self, user, name, *args, **data):
        self.client.force_login(user)
        return self.client.post(reverse(name, args=args or [self.text.pk]), data, follow=True)

    def text_action(self, user, name):
        """Przycisk przejścia redakcji na karcie tekstu (z tokenem stanu tekstu)."""
        self.text.refresh_from_db()
        return self.post(user, name, workflow_token=make_token(self.text, user))

    def open_stage(self, kind):
        return current_stage_queryset(self.text).filter(stage_type=kind, is_completed=False).latest('pk')

    def take(self, user, kind, **data):
        return self.post(user, 'core:take_workflow_stage', self.open_stage(kind).pk, **data)

    def take_and_complete(self, user, kind, *, assigned=False, **data):
        """Przejmuje etap z puli (assigned – już przypisany: tylko rozpoczyna) i kończy go dzisiejszą datą."""
        if assigned:
            self.post(user, 'core:start_assigned_workflow_stage', self.open_stage(kind).pk, started_at=self.day)
        else:
            self.take(user, kind, started_at=self.day)
        return self.post(user, 'core:complete_workflow_stage', self.open_stage(kind).pk, ended_at=self.day, **data)

    @staticmethod
    def began(label):
        return ('stage_completed', f'Rozpoczęto {label}')

    @staticmethod
    def ended(label):
        return ('stage_completed', f'Zakończono {label}')

    def run_steps(self, steps):
        """Wykonuje kroki (opis, akcja, oczekiwane (zdarzenie, [etap])) i sprawdza wiadomości każdego z nich
        – także, że zostały wysłane na Discord (stan „Wysłano”)."""
        for case, action, expected in steps:
            DiscordNotification.objects.all().delete()
            with self.captureOnCommitCallbacks(execute=True):
                response = action()
            with self.subTest(case):
                self.assertEqual(response.status_code, 200)
                rows = DiscordNotification.objects.order_by('pk')
                self.assertEqual(sorted((item.trigger, PREVIEW.sub('', item.content)) for item in rows),
                                 sorted(expected))
                self.assertEqual({item.status for item in rows}, {'sent'} if expected else set())

    def test_main_workflow_through_pages(self):
        """Pełna ścieżka zwykłej antologii: przejęcie tekstu do redakcji i każde wznowienie redakcji po weryfikacji
        – „Rozpoczęto Redakcja”; każde przekazanie redakcji (W1, W2, koordynator) – wiadomość z celem; wysłanie
        do autora i powrót – bez wiadomości; sama rezerwacja W1 – bez wiadomości; W1, W2, kontrole, K1–K4, W3
        i stylowanie – rozpoczęcie i zakończenie."""
        verifier_3 = create_member('weryfikator_3', 'Weryfikator')
        began, ended = self.began, self.ended

        def both(label, *announced):
            return [began(label), ended(label), *announced]
        self.run_steps([
            ('przejęcie redakcji', lambda: self.take(self.editor, 'ready_for_editing', started_at=self.day),
             [('first_verification', 'Pierwsza weryfikacja'), began('Redakcja')]),
            ('rezerwacja W1', lambda: self.take(self.verifier_1, 'first_verification'), []),
            ('redakcja → W1', lambda: self.text_action(self.editor, 'core:send_to_first_verification_stage'),
             [ended('Redakcja → Pierwsza weryfikacja')]),
            ('rozpoczęcie W1', lambda: self.post(self.verifier_1, 'core:start_assigned_workflow_stage',
                                                 self.open_stage('first_verification').pk, started_at=self.day),
             [began('Pierwsza weryfikacja')]),
            ('zakończenie W1', lambda: self.post(self.verifier_1, 'core:complete_workflow_stage',
                                                 self.open_stage('first_verification').pk, ended_at=self.day),
             [ended('Pierwsza weryfikacja')]),
            ('wznowienie redakcji', lambda: self.text_action(self.editor, 'core:resume_text_editing'),
             [began('Redakcja')]),
            ('redakcja → autor', lambda: self.text_action(self.editor, 'core:send_text_to_author_stage'), []),
            ('powrót od autora', lambda: self.text_action(self.editor, 'core:resume_text_editing'), []),
            ('redakcja → W2', lambda: self.text_action(self.editor, 'core:send_to_second_verification_stage'),
             [ended('Redakcja → Druga weryfikacja'), ('second_verification', 'Druga weryfikacja')]),
            ('W2', lambda: self.take_and_complete(self.verifier_2, 'second_verification'), both('Druga weryfikacja')),
            ('wznowienie redakcji', lambda: self.text_action(self.editor, 'core:resume_text_editing'),
             [began('Redakcja')]),
            ('redakcja → koordynator', lambda: self.text_action(self.editor, 'core:finish_text_editing'),
             [ended('Redakcja → Kontrola K. redakcji')]),
            ('kontrola K. redakcji → dalsza redakcja',
             lambda: self.take_and_complete(self.coordinator, 'editing_control', send_to_proofreading='false'),
             both('Kontrola K. redakcji')),
            ('rozpoczęcie dalszej redakcji', lambda: self.post(
                self.editor, 'core:start_assigned_workflow_stage', self.open_stage('editing').pk,
                started_at=self.day), [began('Redakcja')]),
            ('redakcja → koordynator (ponownie)', lambda: self.text_action(self.editor, 'core:finish_text_editing'),
             [ended('Redakcja → Kontrola K. redakcji')]),
            ('kontrola K. redakcji → K1',
             lambda: self.take_and_complete(self.coordinator, 'editing_control', assigned=True,
                                            send_to_proofreading='true'),
             both('Kontrola K. redakcji', ('first_proofreading', 'Pierwsza korekta'))),
            ('K1', lambda: self.take_and_complete(self.proofreader, 'first_proofreading'), both('Pierwsza korekta')),
            ('K2', lambda: self.take_and_complete(self.proofreading_coordinator, 'second_proofreading'),
             both('Druga korekta', ('third_verification', 'Trzecia weryfikacja'))),
            ('W3', lambda: self.take_and_complete(verifier_3, 'third_verification'), both('Trzecia weryfikacja')),
            ('kontrola koordynatora', lambda: self.take_and_complete(self.coordinator, 'coordinator_control'),
             both('Kontr. wer. (koordynator)')),
            ('kontrola redaktora', lambda: self.take_and_complete(self.editor, 'editor_control', assigned=True),
             both('Kontr. wer. (redaktor)', ('third_proofreading', 'Trzecia korekta'))),
            ('K3', lambda: self.take_and_complete(self.other_proofreader, 'third_proofreading'),
             both('Trzecia korekta')),
            ('K4', lambda: self.take_and_complete(self.proofreading_coordinator, 'fourth_proofreading'),
             both('Czwarta korekta')),
            ('stylowanie', lambda: self.take_and_complete(self.superuser, 'styling'), both('Stylowanie')),
        ])

    def to_editing_control(self):
        """Redakcja z W1 i W2 przekazana koordynatorowi, który przejął i rozpoczął kontrolę K. redakcji."""
        self.take(self.editor, 'ready_for_editing', started_at=self.day)
        self.text_action(self.editor, 'core:send_to_first_verification_stage')
        self.take_and_complete(self.verifier_1, 'first_verification')
        self.text_action(self.editor, 'core:resume_text_editing')
        self.text_action(self.editor, 'core:send_to_second_verification_stage')
        self.take_and_complete(self.verifier_2, 'second_verification')
        self.text_action(self.editor, 'core:resume_text_editing')
        self.text_action(self.editor, 'core:finish_text_editing')
        self.take(self.coordinator, 'editing_control', started_at=self.day)

    def finish_control(self):
        """Admin „Daty i przekazanie”: zakończenie kontroli K. redakcji i przekazanie do K1."""
        self.text.refresh_from_db()
        return self.post(self.superuser, 'admin:workflow_stage_dates', self.open_stage('editing_control').pk,
                         started_at=self.day, ended_at=self.day, finish='on', send_to_proofreading='true',
                         version=version_of(self.text))

    def test_admin_finish_actions(self):
        """Admin: „Daty i przekazanie” (zakończenie kontroli K. redakcji) i akcja listy „Zakończ etap i utwórz
        następny” (K1, wcześniej przejęta z rozpoczęciem) – wiadomości jak przy przyciskach na karcie tekstu."""
        self.to_editing_control()

        def list_action():
            stage = self.open_stage('first_proofreading')
            self.take(self.proofreader, 'first_proofreading', started_at=self.day)
            self.client.force_login(self.superuser)
            return self.client.post(reverse('admin:workflow_workflowstage_changelist'),
                                    {'action': 'finish_selected_stages', '_selected_action': [stage.pk]}, follow=True)

        self.run_steps([
            ('Daty i przekazanie', self.finish_control,
             [self.ended('Kontrola K. redakcji'), ('first_proofreading', 'Pierwsza korekta')]),
            ('akcja „Zakończ etap”', list_action, [self.began('Pierwsza korekta'), self.ended('Pierwsza korekta')]),
        ])

    def test_admin_start_date_starts_the_stage_once(self):
        """Admin „Daty i przekazanie”: data rozpoczęcia zarezerwowanej, nierozpoczętej W1 to rozpoczęcie etapu
        („Rozpoczęto”); ponowny zapis daty rozpoczętego etapu to poprawka danych – bez wiadomości."""
        self.take(self.editor, 'ready_for_editing', started_at=self.day)
        self.take(self.verifier_1, 'first_verification')
        self.text_action(self.editor, 'core:send_to_first_verification_stage')

        def dates():
            self.text.refresh_from_db()
            return self.post(self.superuser, 'admin:workflow_stage_dates', self.open_stage('first_verification').pk,
                             started_at=self.day, version=version_of(self.text))

        self.run_steps([
            ('rozpoczęcie z admina', dates, [self.began('Pierwsza weryfikacja')]),
            ('ponowny zapis daty rozpoczętego etapu', dates, []),
        ])
        self.assertEqual(self.open_stage('first_verification').started_at, self.today)

    def test_admin_history_corrections_send_nothing(self):
        """Uzupełnianie i poprawianie historii w adminie (brakujący etap – historyczny i z datami, korekta dat
        zakończonego etapu, cofnięcie zakończenia, korekta wykonawcy) to korekta danych, nie zdarzenie: bez
        rozpoczęć i zakończeń etapów i bez ogłoszeń „do wzięcia”, choć zmiana trafia do dziennika zmian workflow. Ponowne
        zakończenie cofniętego etapu formularzem „Daty i przekazanie” wysyła wiadomości jak zwykle."""
        self.to_editing_control()
        self.finish_control()
        stages = WorkflowStage.objects.filter(text=self.text)

        def add_stage(kind, performer, **data):
            self.text.refresh_from_db()
            return self.post(self.superuser, 'admin:texts_text_add_stage', self.text.pk, kind=kind,
                             performer=performer.pk, version=version_of(self.text), **data)

        def stage_dates(kind):
            self.text.refresh_from_db()
            stage = stages.get(stage_type=kind)
            return self.post(self.superuser, 'admin:workflow_stage_dates', stage.pk, started_at=stage.started_at,
                             ended_at=self.day, version=version_of(self.text))

        def correct(kind, action, **data):
            self.text.refresh_from_db()
            return self.post(self.superuser, 'admin:workflow_stage_correct', stages.get(stage_type=kind).pk,
                             action=action, confirm='on', version=version_of(self.text), **data)

        steps = [
            ('cofnięcie zakończenia', lambda: correct('editing_control', 'reopen'),
             lambda: not stages.get(stage_type='editing_control').is_completed),
            ('korekta wykonawcy', lambda: correct('editing_control', 'performer', performer=self.superuser.pk),
             lambda: stages.get(stage_type='editing_control').assignment.assigned_to == self.superuser),
            ('korekta dat zakończonego etapu', lambda: stage_dates('second_verification'),
             lambda: stages.get(stage_type='second_verification').ended_at == self.today),
            ('brakujący etap historyczny', lambda: add_stage('third_verification', self.verifier_1, historical='on'),
             lambda: stages.filter(stage_type='third_verification', is_completed=True).exists()),
            ('brakujący etap z datami', lambda: add_stage('second_proofreading', self.proofreading_coordinator,
                                                          started_at=self.day, ended_at=self.day),
             lambda: stages.filter(stage_type='second_proofreading', is_completed=True).exists()),
        ]
        events = WorkflowEvent.objects.count()
        for case, action, done in steps:
            self.run_steps([(case, action, [])])
            with self.subTest(case, check='operacja wykonana'):
                self.assertTrue(done())
            if case == 'korekta wykonawcy':
                self.run_steps([('ponowne zakończenie kontroli', self.finish_control,
                                 [self.ended('Kontrola K. redakcji'), ('first_proofreading', 'Pierwsza korekta')])])
        self.assertGreater(WorkflowEvent.objects.count(), events)

    def test_extracts_workflow_through_pages(self):
        """Ekstrakty (Redakcja → W1 → K1 → Gotowe): przejęcie tomu do redakcji – „Rozpoczęto Redakcja”,
        przekazanie redakcji do W1 – wiadomość z celem, W1 i K1 – rozpoczęcie i zakończenie, „Gotowe” – nic."""
        book = Anthology.objects.create(title='Ekstrakty 9', is_extracts=True)
        self.text = book.texts.get()
        began, ended = self.began, self.ended
        self.run_steps([
            ('przejęcie redakcji', lambda: self.take(self.editor, 'ready_for_editing', started_at=self.day),
             [('first_verification', 'Pierwsza weryfikacja'), began('Redakcja')]),
            ('redakcja → W1', lambda: self.text_action(self.editor, 'core:send_to_first_verification_stage'),
             [ended('Redakcja → Pierwsza weryfikacja'), ('first_verification', 'Pierwsza weryfikacja')]),
            ('W1 → K1', lambda: self.take_and_complete(self.verifier_1, 'first_verification'),
             [began('Pierwsza weryfikacja'), ended('Pierwsza weryfikacja'), ('first_proofreading', 'Pierwsza korekta')]),
            ('K1 → Gotowe', lambda: self.take_and_complete(self.proofreader, 'first_proofreading'),
             [began('Pierwsza korekta'), ended('Pierwsza korekta')]),
        ])


Setting = Anthology.DiscordNotifications


@override_settings(DISCORD_WEBHOOKS={'Kanał': URL}, CMS_BASE_URL='https://cms.example')
class AnthologyNotificationSettingTests(WorkflowTestDataMixin, TestCase):
    """Ustawienie antologii „Powiadomienia Discord” (także dla powieści i Ekstraktów – to samo pole): „Włączone”
    – wszystko, „Tylko rozpoczęcia i zakończenia etapów” – tylko ta reguła, „Wyłączone” – nic o tekstach antologii
    (do wzięcia, rozpoczęcie i zakończenie, przypomnienie, audiobook). Tekst bez antologii – jak „Włączone”."""

    def setUp(self):
        super().setUp()
        for trigger in NotificationRule.Trigger.values:
            enable(trigger, '[etap]')
        sender = patch('core.discord_webhook.send_message', return_value='1')
        self.addCleanup(sender.stop)
        sender.start()

    def triggers(self, action):
        """Reguły wiadomości powstałych w tej operacji (bez wcześniejszych wpisów historii)."""
        DiscordNotification.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            action()
        return sorted(DiscordNotification.objects.exclude(content='ogłoszenie').values_list('trigger', flat=True))

    def reminder(self):
        """Ogłoszenie W1 sprzed 8 dni (wysłane, gdy ustawienie jeszcze pozwalało) i codzienne przypomnienia."""
        DiscordNotification.objects.create(
            trigger='first_verification', channel='Kanał', content='ogłoszenie', status='sent', text=self.text,
            stage=self.stage(StageType.FIRST_VERIFICATION))
        DiscordNotification.objects.filter(content='ogłoszenie').update(created_at=timezone.now() - timedelta(days=8))
        send_waiting_reminders()

    def test_messages_for_each_setting(self):
        """Tabela: ustawienie (i tekst bez antologii) × rodzaj wiadomości → reguły, z których powstały wiadomości."""
        everything = {
            'przejęcie do redakcji': ['first_verification', 'stage_completed'],
            'przypomnienie': ['stage_waiting'],
            'przekazanie do W1': ['first_verification', 'stage_completed'],
            'audiobook': ['audiobook_production'],
        }
        expected = {
            Setting.ALL: everything,
            Setting.STAGES: {'przejęcie do redakcji': ['stage_completed'], 'przypomnienie': [],
                             'przekazanie do W1': ['stage_completed'], 'audiobook': []},
            Setting.OFF: dict.fromkeys(everything, []),
            None: everything,
        }
        for setting, cases in expected.items():
            self.text = Text.objects.create(title=f'Tekst {setting}', length=100, anthology=None if setting is None else
                                            Anthology.objects.create(title=f'A {setting}', discord_notifications=setting))
            WorkflowStage.objects.create(text=self.text, workflow_cycle=1, stage_type=StageType.READY_FOR_EDITING)
            steps = {
                'przejęcie do redakcji': self.begin_editing,
                'przypomnienie': self.reminder,
                'przekazanie do W1': lambda: send_to_first_verification(self.text, self.editor, ended_at=self.today),
                'audiobook': lambda: audiobook_changed(self.text, actor='Jan', status='Montaż', change='Zmiana.'),
            }
            for case, action in steps.items():
                with self.subTest(setting or 'bez antologii', case=case):
                    self.assertEqual(self.triggers(action), cases[case])

    def test_setting_changed_while_stages_are_open(self):
        """Zmiana ustawienia w trakcie procesu działa od razu dla kolejnych zdarzeń: po włączeniu powiadomień
        przekazanie do W1 jest ogłaszane i przypominane, po wyłączeniu przypomnienia i zakończenie etapu milkną;
        Ekstrakty i powieść podlegają temu samemu polu."""
        books = {'antologia': Anthology.objects.create(title='Zmienna', discord_notifications=Setting.OFF),
                 'Ekstrakty': Anthology.objects.create(title='Ekstrakty 9', is_extracts=True,
                                                       discord_notifications=Setting.OFF),
                 'powieść': Anthology.objects.create(title='Powieść', is_novel=True, discord_notifications=Setting.OFF)}
        for case, book in books.items():
            with self.subTest(case):
                self.text = book.texts.first() or Text.objects.create(
                    title=f'Tekst {case}', length=100, anthology=book, chapter_number=1 if book.is_novel else None)
                WorkflowStage.objects.filter(text=self.text).delete()
                WorkflowStage.objects.create(text=self.text, workflow_cycle=1, stage_type=StageType.READY_FOR_EDITING)
                self.assertEqual(self.triggers(self.begin_editing), [])
                Anthology.objects.filter(pk=book.pk).update(discord_notifications=Setting.ALL)
                self.assertEqual(self.triggers(lambda: send_to_first_verification(self.text, self.editor,
                                                                                  ended_at=self.today)),
                                 ['first_verification', 'stage_completed'])
                DiscordNotification.objects.update(created_at=timezone.now() - timedelta(days=8))
                self.assertEqual(send_waiting_reminders(), 1)
                Anthology.objects.filter(pk=book.pk).update(discord_notifications=Setting.OFF)
                DiscordNotification.objects.filter(trigger='stage_waiting').delete()
                self.assertEqual(send_waiting_reminders(), 0)
                stage = claim_stage(self.text, StageType.FIRST_VERIFICATION, self.verifier_1)
                self.assertEqual(self.triggers(lambda: complete_stage(stage, self.verifier_1, self.today)), [])

    def test_review_notifications_ignore_anthology_setting(self):
        """Zgłoszenie do recenzji (bez tekstu) wychodzi także z antologii z wyłączonymi powiadomieniami."""
        book = Anthology.objects.create(title='Cicha', discord_notifications=Setting.OFF)
        with self.captureOnCommitCallbacks(execute=True):
            Review.objects.create(title='Zgłoszenie', genre='horror', length=1000, email='a@example.com',
                                  author_first_name='Jan', author_last_name='Nowak', anthology=book)
        self.assertEqual(list(DiscordNotification.objects.values_list('trigger', flat=True)), ['review_new'])


class AnthologyNotificationFormTests(AnthologyPageData, TestCase):
    """Pole „Powiadomienia Discord” na stronie antologii (także Ekstraktów), na stronie powieści i w panelu admina
    antologii – zmienia je tylko superuser."""

    def save_on_page(self, book, value, user=None):
        user = user or self.admin
        self.client.force_login(user)
        if book.is_novel:
            return self.client.post(reverse('core:novel_detail', args=[book.pk]), {
                'action': 'notifications', 'novel_token': novel_edit_token(book, user),
                'discord-discord_notifications': value})
        return self.client.post(reverse('core:anthology_detail', args=[book.pk]), {
            'action': 'notifications', '_edit_version': edit_token(user, book),
            'discord-discord_notifications': value})

    def test_pages_save_setting(self):
        """Superuser zapisuje każde z trzech ustawień na stronie antologii, Ekstraktów i powieści."""
        books = {'antologia': self.book, 'Ekstrakty': Anthology.objects.create(title='Ekstrakty 9', is_extracts=True),
                 'powieść': Anthology.objects.create(title='Powieść', is_novel=True)}
        for case, book in books.items():
            for value in (Setting.OFF, Setting.STAGES, Setting.ALL):
                with self.subTest(case, value=value):
                    self.assertEqual(self.save_on_page(book, value).status_code, 302)
                    book.refresh_from_db()
                    self.assertEqual(book.discord_notifications, value)
            with self.subTest(case, check='formularz widoczny dla superusera'):
                url = reverse('core:novel_detail' if book.is_novel else 'core:anthology_detail', args=[book.pk])
                self.assertContains(self.client.get(url), 'name="discord-discord_notifications"')

    def test_only_superuser_changes_setting(self):
        """Tabela uprawnień na stronie antologii, powieści i w panelu admina: formularz i zapis tylko dla superusera;
        koordynator i koordynator rekrutacji widzą ustawienie do odczytu, redaktor – wcale. Zapis innych osób
        kończy się 403 (panel admina – przekierowaniem do logowania) i nie zmienia wartości; zapis superusera
        bez wersji formularza – 409."""
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        recruiter = create_member('anthology-recruiter', 'Koordynator rekrutacji')
        people = {'superuser': (self.admin, 'formularz'), 'koordynator': (self.manager, 'odczyt'),
                  'koordynator rekrutacji': (recruiter, 'odczyt'), 'redaktor': (self.member, None)}
        for book in (self.book, novel):
            page = reverse('core:novel_detail' if book.is_novel else 'core:anthology_detail', args=[book.pk])
            for name, (user, view) in people.items():
                with self.subTest(book.title, user=name):
                    Anthology.objects.filter(pk=book.pk).update(discord_notifications=Setting.ALL)
                    self.client.force_login(user)
                    content = self.client.get(page).content.decode()
                    self.assertEqual('name="discord-discord_notifications"' in content, view == 'formularz')
                    self.assertEqual('Ustawienie: <strong>Włączone</strong>' in content, view == 'odczyt')
                    self.assertEqual(self.save_on_page(book, Setting.OFF, user).status_code,
                                     302 if view == 'formularz' else 403)
                    book.refresh_from_db()
                    self.assertEqual(book.discord_notifications, Setting.OFF if view == 'formularz' else Setting.ALL)
        admin_url = reverse('admin:texts_anthology_change', args=[self.book.pk])
        for name, (user, view) in people.items():
            if view == 'formularz':
                continue  # zapis superusera w adminie: test_admin_saves_setting
            with self.subTest('panel admina', user=name):
                self.client.force_login(user)
                for response in (self.client.get(admin_url),
                                 self.client.post(admin_url, {'title': self.book.title, 'discord_notifications': 'off'})):
                    self.assertRedirects(response, f"{reverse('admin:login')}?next={admin_url}",
                                         fetch_redirect_response=False)
                self.book.refresh_from_db()
                self.assertEqual(self.book.discord_notifications, Setting.ALL)
        self.client.force_login(self.admin)
        response = self.client.post(self.url, {'action': 'notifications', 'discord-discord_notifications': 'off'})
        self.assertEqual(response.status_code, 409)
        self.book.refresh_from_db()
        self.assertEqual(self.book.discord_notifications, Setting.ALL)

    def test_admin_saves_setting(self):
        """Formularz antologii w panelu admina zapisuje ustawienie powiadomień."""
        self.client.force_login(self.admin)
        url = reverse('admin:texts_anthology_change', args=[self.book.pk])
        data = admin_form_data(self.client, url, 'anthology_form', _save='Zapisz', discord_notifications='stages')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.book.refresh_from_db()
        self.assertEqual(self.book.discord_notifications, Setting.STAGES)


@override_settings(DISCORD_WEBHOOKS={'Kanał': 'https://discord.com/api/webhooks/1/tajny'})
class DiagnoseCommandTests(TestCase):
    """Komenda discord_diagnose: stan reguł, historia wysyłek i dziennik zmian – bez adresów webhooków
    i bez danych osób (tylko tytuły tekstów)."""

    def test_report(self):
        DiscordRole.objects.create(name='korektor', role_id='555')
        enable('stage_completed', '[osoba]: [etap]')
        NotificationRule.objects.filter(trigger='stage_completed').update(enabled=False)
        enable('first_proofreading', '[etap]', '@korektor')
        enable('third_proofreading', '[etap]', '@nikt')
        NotificationRule.objects.filter(trigger='third_proofreading').update(channel='Dawny')
        text = Text.objects.create(title='Latarnik', length=1)
        DiscordNotification.objects.create(trigger='first_proofreading', channel='Kanał', content='Jan Tajny',
                                           status='sent', text=text)
        DiscordNotification.objects.create(trigger='stage_completed', channel='Kanał', content='[TEST] Jan Tajny',
                                           status='disabled')
        WorkflowEvent.objects.create(text=text, title='Latarnik', actor_name='Jan Tajny', previous_status='a',
                                     next_status='b', details='Zakończono etap: Redakcja – wykonanie 1 – Jan Tajny')
        Anthology.objects.create(title='Zwykła')
        Anthology.objects.create(title='Cicha', discord_notifications=Setting.OFF)
        Anthology.objects.create(title='Powieść etapowa', is_novel=True, discord_notifications=Setting.STAGES)
        out = StringIO()
        call_command('discord_diagnose', stdout=out)
        report = out.getvalue()
        expected = [
            'Kanały na serwerze (DISCORD_WEBHOOKS): Kanał',
            'Rozpoczęcie i zakończenie etapu (wszystkie etapy) [stage_completed]: włączona: nie; kanał: „Kanał” '
            '(w konfiguracji serwera: tak); treści: 1; oznaczenie: brak',
            'Uwagi: WYŁĄCZONE – test z panelu wysyła mimo to, ale zwykłe zdarzenia nie',
            '[first_proofreading]: włączona: tak; kanał: „Kanał” (w konfiguracji serwera: tak); treści: 1; '
            'oznaczenie: @korektor (poprawne)\n  Działa.',
            'kanał: „Dawny” (w konfiguracji serwera: nie)', 'oznaczenie: @nikt (BŁĘDNE – nie ma takiej roli)',
            '– Wysłano – „Latarnik”', '– Brak skonfigurowanego kanału – test z panelu',
            'Antologie z ograniczonymi powiadomieniami:\n- „Cicha”: Wyłączone\n'
            '- „Powieść etapowa”: Tylko rozpoczęcia i zakończenia etapów\n\n',
            'ostatnie 7 dni: 1 zmian, w tym 1 z zakończeniem etapu; wpisy „Rozpoczęcie i zakończenie etapu (wszystkie '
            'etapy)” w historii wysyłek: 1.',
        ]
        for text_part in expected:
            with self.subTest(text_part):
                self.assertIn(text_part, report)
        for secret in ('tajny', 'Tajny', '555', 'Zwykła'):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, report)

    def test_broken_or_empty_server_configuration(self):
        """Brak kanałów w .env – „brak”; błędna konfiguracja – ostrzeżenie, kanał reguły bez oceny, uwaga przy
        regule; zgłoszenie do recenzji w historii bez tytułu ani danych osoby."""
        enable('first_proofreading', '[etap]')
        review = Review.objects.create(anthology=Anthology.objects.create(title='Nabór'), title='Tajny tytuł',
                                       author_first_name='Jan', author_last_name='Tajny', email='jan@example.com',
                                       genre='fantasy', length=10)
        DiscordNotification.objects.create(trigger='review_new', channel='Kanał', content='x', status='sent',
                                           review=review)
        cases = {
            'brak kanałów': ({}, ['Kanały na serwerze (DISCORD_WEBHOOKS): brak', 'kanał: „Kanał” (w konfiguracji serwera: nie)',
                                  'kanału „Kanał” nie ma w konfiguracji serwera']),
            'błędna konfiguracja': (['nie słownik'], ['BŁĘDNA KONFIGURACJA – żadna wiadomość nie wyjdzie',
                                                     'kanał: „Kanał”; treści: 1',
                                                     'Uwagi: błędna konfiguracja kanałów na serwerze (DISCORD_WEBHOOKS)']),
        }
        for case, (webhooks, expected) in cases.items():
            with self.subTest(case), override_settings(DISCORD_WEBHOOKS=webhooks):
                out = StringIO()
                call_command('discord_diagnose', stdout=out)
                report = out.getvalue()
                for text_part in expected:
                    self.assertIn(text_part, report)
                self.assertIn('– Wysłano – zgłoszenie do recenzji', report)
                self.assertNotIn('Tajny', report)

    def test_hint_when_stages_end_without_messages(self):
        """Etapy kończą się (dziennik zmian), a wiadomości „Rozpoczęcie i zakończenie etapu” brak – komenda podpowiada
        przyczynę."""
        WorkflowEvent.objects.create(title='Latarnik', actor_name='x', previous_status='a', next_status='b',
                                     details='Zakończono etap: Pierwsza korekta')
        for sent, hint in ((False, True), (True, False)):
            if sent:
                DiscordNotification.objects.create(trigger='stage_completed', channel='Kanał', content='x')
            out = StringIO()
            call_command('discord_diagnose', stdout=out)
            with self.subTest(sent=sent):
                self.assertEqual('nie powstała żadna wiadomość „Rozpoczęcie i zakończenie etapu' in out.getvalue(), hint)
