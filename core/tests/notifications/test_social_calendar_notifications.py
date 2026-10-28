"""Powiadomienie Discord „Nowy wpis w kalendarzu Social Media”: domyślna reguła, treść z tagami i odnośnikiem do
dnia w kalendarzu, wysyłka po zatwierdzeniu transakcji tylko przy nowym wpisie, reguła wyłączona albo bez kanału,
testowa wysyłka z panelu admina. Ruch do Discorda jest zawsze zamockowany."""
import datetime
from unittest.mock import patch

from django.test import TestCase, override_settings, tag
from django.urls import reverse

from core.discord_notifications import calendar_day_label
from core.edit_versions import version_of
from core.models import DiscordNotification, DiscordRole, NotificationRule, SocialCalendarEntry
from core.notification_models import TEMPLATE_TAGS, validate_template_tags
from core.views.social_media import _token
from fantazmaty.testing import create_member, create_superuser
from people import roles

WEBHOOK = 'https://discord.com/api/webhooks/123/abc_DEF-1'
CALENDAR = reverse('core:social_calendar')
LINK = f'[O, tutaj](<https://cms.example{CALENDAR}?month=2026-11#social-day-2026-11-20>)'
TRIGGER = 'social_calendar'


def enable(template='[data] ([platforma]): „[opis]” – [osoba]', mention='', channel='Kanał'):
    rule = NotificationRule.objects.get(trigger=TRIGGER)
    rule.enabled, rule.channel, rule.mention = True, channel, mention
    rule.save()
    rule.templates.all().delete()
    rule.templates.create(content=template)
    return rule


class SocialCalendarRuleDefaultTests(TestCase):
    def test_migration_creates_disabled_rule_with_valid_default_text(self):
        """Migracja dodaje regułę „Nowy wpis w kalendarzu Social Media” – wyłączoną, bez kanału i oznaczenia, z domyślną
        treścią z tagami kalendarza; tagi [data], [opis] i [platforma] są opisane i przechodzą walidację."""
        rule = NotificationRule.objects.get(trigger=TRIGGER)
        self.assertEqual((rule.enabled, rule.channel, rule.mention, str(rule)),
                         (False, '', '', 'Nowy wpis w kalendarzu Social Media'))
        templates = list(rule.templates.values_list('content', flat=True))
        self.assertEqual(templates, ['Nowy wpis w kalendarzu Social Media na [data] ([platforma]): „[opis]”. '
                                     'Dodał(a): [osoba].'])
        validate_template_tags(templates[0])
        for name in ('data', 'opis', 'platforma'):
            with self.subTest(tag=name):
                self.assertIn('kalendarz', TEMPLATE_TAGS[name])

    def test_day_label(self):
        """[data]: dzień tygodnia małą literą i data (dzień bez zera wiodącego, miesiąc dwucyfrowo)."""
        cases = ((datetime.date(2026, 11, 20), 'piątek, 20.11.2026'), (datetime.date(2028, 2, 29), 'wtorek, 29.02.2028'),
                 (datetime.date(2026, 3, 1), 'niedziela, 1.03.2026'))
        for day, expected in cases:
            with self.subTest(day=day):
                self.assertEqual(calendar_day_label(day), expected)


@tag('external')
@override_settings(DISCORD_WEBHOOK_SECRETS={'Kanał': WEBHOOK}, CMS_BASE_URL='https://cms.example/')
class SocialCalendarNotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.social = create_member('notify-social', roles.SOCIAL_MEDIA)
        DiscordRole.objects.create(name='social', role_id='777')

    def setUp(self):
        self.client.force_login(self.social)

    def add(self, **data):
        return self.client.post(CALENDAR, {'action': 'add', 'day': '2026-11-20', 'description': 'Cytat z „Latarnika”',
                                           'platform': 'facebook_instagram', 'month': '2026-11', **data})

    def test_new_entry_is_queued_with_tags_and_link_and_sent_after_commit(self):
        """Nowy wpis: wiadomość z tagami [data], [platforma], [opis], [osoba], oznaczeniem roli i odnośnikiem „O, tutaj”
        do dnia w kalendarzu trafia do historii od razu, a do Discorda dopiero po zatwierdzeniu transakcji."""
        enable(mention='@social')
        with patch('core.discord_webhook.send_message', return_value='55') as sent:
            with self.captureOnCommitCallbacks() as callbacks:
                response = self.add()
            self.assertEqual(response.status_code, 302)
            item = DiscordNotification.objects.get()
            self.assertEqual(item.status, 'pending')
            sent.assert_not_called()
            for callback in callbacks:
                callback()
        expected = (f'<@&777> piątek, 20.11.2026 (Facebook i Instagram): „Cytat z „Latarnika”” – notify-social Testowy '
                    f'{LINK}')
        sent.assert_called_once_with(WEBHOOK, expected, role_ids=['777'], here=False, suppress_embeds=True)
        item.refresh_from_db()
        self.assertEqual((item.trigger, item.channel, item.content, item.status, item.message_id, item.text_id),
                         (TRIGGER, 'Kanał', expected, 'sent', '55', None))

    def test_link_tag_position(self):
        """[link] w treści – odnośnik w tym miejscu (do dnia wpisu), nie na końcu."""
        enable('[link] – [opis]')
        with patch('core.discord_webhook.send_message', return_value='1'), self.captureOnCommitCallbacks(execute=True):
            self.add()
        self.assertEqual(DiscordNotification.objects.get().content, f'{LINK} – Cytat z „Latarnika”')

    def test_only_new_entries_and_only_from_working_rule(self):
        """Bez wiadomości: edycja i usunięcie wpisu, reguła wyłączona, bez kanału albo bez treści – wpis i tak się
        zapisuje."""
        enable()
        entry = SocialCalendarEntry.objects.create(day=datetime.date(2026, 11, 20), description='Stary',
                                                   added_by=self.social)
        for action in ('edit', 'delete'):
            with self.subTest(action=action), patch('core.discord_webhook.send_message') as sent:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(CALENDAR, {
                        'action': action, 'entry': entry.pk, 'day': '2026-11-21', 'description': 'Nowy opis',
                        'platform': 'other', 'version': _token(self.social, 'calendar', entry.pk, version_of(entry))})
                self.assertEqual(response.status_code, 302)
                sent.assert_not_called()
        self.assertFalse(DiscordNotification.objects.exists())
        cases = {'wyłączona': {'enabled': False}, 'bez kanału': {'channel': ''}, 'bez treści': {'templates': False}}
        for case, change in cases.items():
            with self.subTest(case), patch('core.discord_webhook.send_message') as sent:
                rule = enable()
                NotificationRule.objects.filter(pk=rule.pk).update(enabled=change.get('enabled', True),
                                                                   channel=change.get('channel', 'Kanał'))
                if change.get('templates') is False:
                    rule.templates.all().delete()
                with self.captureOnCommitCallbacks(execute=True):
                    self.assertEqual(self.add(description=case).status_code, 302)
                self.assertTrue(SocialCalendarEntry.objects.filter(description=case).exists())
                sent.assert_not_called()
                self.assertFalse(DiscordNotification.objects.exists())

    def test_queue_or_delivery_error_never_blocks_entry(self):
        """Błąd kolejkowania albo wysyłki nie cofa wpisu w kalendarzu; nieudana wysyłka zostaje w historii."""
        enable()
        with patch('core.discord_notifications.queue', side_effect=RuntimeError('x')), \
                self.assertLogs('core.discord_notifications', 'ERROR'):
            self.assertEqual(self.add(description='Mimo błędu kolejki').status_code, 302)
        self.assertTrue(SocialCalendarEntry.objects.filter(description='Mimo błędu kolejki').exists())
        from core.discord_webhook import DeliveryError
        with patch('core.discord_webhook.send_message', side_effect=DeliveryError('odmowa')), \
                self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.add(description='Mimo błędu wysyłki').status_code, 302)
        self.assertTrue(SocialCalendarEntry.objects.filter(description='Mimo błędu wysyłki').exists())
        self.assertEqual(DiscordNotification.objects.get().status, 'failed')


@tag('external')
@override_settings(DISCORD_WEBHOOK_SECRETS={'Kanał': WEBHOOK}, CMS_BASE_URL='https://cms.example')
class SocialCalendarAdminTests(TestCase):
    def setUp(self):
        self.admin = create_superuser('calendar-notify-admin')
        self.client.force_login(self.admin)

    def test_rule_page_and_test_panel(self):
        """Strona reguły wyjaśnia, kiedy wychodzi wiadomość; panel testowy ma przycisk tej reguły i wysyła od razu
        treść z polami [data], [opis], [platforma], [osoba] oraz odnośnikiem do dnia w kalendarzu (bez tagów tekstu)."""
        rule = enable('[data] [platforma] [opis] [osoba] [tytuł]', channel='Kanał')
        page = self.client.get(reverse('admin:core_notificationrule_change', args=[rule.pk]))
        self.assertContains(page, 'Jedna wiadomość po dodaniu wpisu do kalendarza Social Media')
        url = reverse('admin:core_notificationrule_test')
        panel = self.client.get(url)
        self.assertContains(panel, 'Nowy wpis w kalendarzu Social Media')
        self.assertContains(panel, '[platforma] (dla kalendarza Social Media)')
        data = {'title': 'Latarnik', 'person': 'Ewa Lis', 'calendar_day': '2026-11-20', 'calendar_description': 'Cytat',
                'calendar_platform': 'instagram', 'trigger': TRIGGER, 'mark_test': 'on'}
        with patch('core.discord_webhook.send_message', return_value='9') as sent:
            response = self.client.post(url, data)
        self.assertContains(response, 'Wysłano na kanał')
        sent.assert_called_once_with(WEBHOOK, f'[TEST] piątek, 20.11.2026 Instagram Cytat Ewa Lis [tytuł] {LINK}',
                                     role_ids=[], here=False, suppress_embeds=True)
        self.assertEqual(DiscordNotification.objects.get().trigger, TRIGGER)

    def test_test_panel_defaults_to_today(self):
        """Bez daty w panelu testowym [data] to dzisiejszy dzień (strefa Europe/Warsaw)."""
        enable('[data]')
        moment = datetime.datetime(2026, 10, 31, 23, 30, tzinfo=datetime.timezone.utc)
        with patch('django.utils.timezone.now', return_value=moment), \
                patch('core.discord_webhook.send_message', return_value='9') as sent:
            # Logowanie w zamrożonym czasie: sesja (7 dni) nie wygasa niezależnie od daty uruchomienia testów.
            self.client.force_login(self.admin)
            self.client.post(reverse('admin:core_notificationrule_test'), {'title': 'X', 'trigger': TRIGGER})
        self.assertTrue(sent.call_args.args[1].startswith('niedziela, 1.11.2026 [O, tutaj]'), sent.call_args.args[1])
