"""Powiadomienia Discord: tylko skonfigurowane adresy HTTPS, bez wzmianek, z jasnym wynikiem wysyłki zapisanym
przy zdarzeniu workflow; błąd wysyłki nigdy nie psuje zatwierdzonej operacji."""
import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase, TestCase, override_settings

from core.discord_webhook import ConfigurationError, DeliveryError, channels, send_message
from core.models import WorkflowEvent
from core.workflow_events import deliver, message, notify_after_commit

URL = 'https://discord.com/api/webhooks/123/abc_DEF-1'


def response(status=200, body=None):
    handle = MagicMock()
    handle.status = status
    handle.read.return_value = json.dumps(body if body is not None else {'id': '999'}).encode()
    handle.__enter__.return_value = handle
    return handle


class ChannelConfigTests(SimpleTestCase):
    @override_settings(DISCORD_WEBHOOKS={'Testowy': URL})
    def test_valid_configuration(self):
        self.assertEqual(channels(), {'Testowy': URL})

    def test_rejects_foreign_hosts_and_bad_shapes(self):
        for value in ({'x': 'https://evil.example/api/webhooks/1/a'}, {'x': 'http://discord.com/api/webhooks/1/a'},
                      {'': URL}, ['not', 'a', 'dict'], {f'k{i}': URL for i in range(21)}):
            with self.subTest(value=str(value)[:40]), override_settings(DISCORD_WEBHOOKS=value):
                with self.assertRaises(ConfigurationError):
                    channels()

    @override_settings()
    def test_environment_json_is_parsed(self):
        from django.conf import settings
        del settings.DISCORD_WEBHOOKS
        with patch.dict('os.environ', {'DISCORD_WEBHOOKS': json.dumps({'Kanał': URL})}):
            self.assertEqual(channels(), {'Kanał': URL})
        with patch.dict('os.environ', {'DISCORD_WEBHOOKS': '{broken'}), self.assertRaises(ConfigurationError):
            channels()


class SendMessageTests(SimpleTestCase):
    def opener(self, result=None, error=None):
        opener = MagicMock()
        if error:
            opener.open.side_effect = error
        else:
            opener.open.return_value = result
        return patch('core.discord_webhook.build_opener', return_value=opener), opener

    def test_success_returns_message_id_and_disables_mentions(self):
        patcher, opener = self.opener(response())
        with patcher:
            self.assertEqual(send_message(URL, 'Treść'), '999')
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith('?wait=true'))
        self.assertEqual(json.loads(request.data)['allowed_mentions'], {'parse': []})

    def test_invalid_url_is_refused_before_network(self):
        with patch('core.discord_webhook.build_opener') as build:
            with self.assertRaises(DeliveryError):
                send_message('https://example.com/hook', 'x')
            build.assert_not_called()

    def test_missing_id_is_uncertain(self):
        patcher, _ = self.opener(response(body={}))
        with patcher, self.assertRaises(DeliveryError) as caught:
            send_message(URL, 'x')
        self.assertTrue(caught.exception.uncertain)

    def test_http_errors_map_to_certain_or_uncertain_failures(self):
        for code, uncertain in ((429, False), (404, False), (400, False), (502, True)):
            error = HTTPError(URL, code, 'x', {}, BytesIO(b''))
            patcher, _ = self.opener(error=error)
            with self.subTest(code=code), patcher, self.assertRaises(DeliveryError) as caught:
                send_message(URL, 'x')
            self.assertEqual(caught.exception.uncertain, uncertain)

    def test_network_error_is_uncertain(self):
        patcher, _ = self.opener(error=URLError('down'))
        with patcher, self.assertRaises(DeliveryError) as caught:
            send_message(URL, 'x')
        self.assertTrue(caught.exception.uncertain)


@override_settings(DISCORD_WEBHOOKS={'Testowy': URL})
class WorkflowNotificationTests(TestCase):
    """Zdarzenie workflow jest wysyłane raz, a wynik (także niepewny) zostaje zapisany."""

    def event(self, **fields):
        data = dict(title='Tytuł', authors='Jan Autor', actor_name='Redaktor', previous_status='Redakcja',
                    next_status='Weryfikacja', details='Zakończono etap: Redakcja', channel='Testowy')
        return WorkflowEvent.objects.create(**{**data, **fields})

    def delivered(self, event, **send):
        with patch('core.discord_webhook.send_message', **send) as sender:
            deliver(event.pk)
        event.refresh_from_db()
        return event, sender

    def test_message_shows_status_change_and_stays_within_discord_limit(self):
        event = self.event()
        self.assertEqual(message(event), 'Tekst: Tytuł\nAutor: Jan Autor\nStatus: Redakcja → Weryfikacja\n'
                                         'Wykonał(a): Redaktor\nZakończono etap: Redakcja')
        long = self.event(title='T' * 3000, previous_status='Redakcja', next_status='Redakcja', details='x' * 3000)
        text = message(long)
        self.assertIn('Status: Redakcja\n', text)
        self.assertLessEqual(len(text), 2000)

    def test_successful_delivery_records_message_id_and_is_not_repeated(self):
        event, sender = self.delivered(self.event(), return_value='123')
        self.assertEqual((event.status, event.message_id), ('sent', '123'))
        self.assertEqual(sender.call_args.args[0], URL)
        _, sender = self.delivered(event, return_value='456')
        sender.assert_not_called()

    def test_unconfigured_channel_is_disabled_without_sending(self):
        event, sender = self.delivered(self.event(channel='Inny'))
        self.assertEqual(event.status, 'disabled')
        sender.assert_not_called()

    def test_failures_are_recorded_as_failed_or_unknown(self):
        for error, expected in ((DeliveryError('odrzucono'), 'failed'),
                                (DeliveryError('brak potwierdzenia', uncertain=True), 'unknown')):
            with self.subTest(expected=expected):
                event, _ = self.delivered(self.event(), side_effect=error)
                self.assertEqual(event.status, expected)
        # Nieoczekiwany błąd: wynik niepewny, a w logu nie ma adresu webhooka.
        with self.assertLogs('core.workflow_events', 'ERROR') as logs:
            event, _ = self.delivered(self.event(), side_effect=RuntimeError(URL))
        self.assertEqual(event.status, 'unknown')
        self.assertNotIn('abc_DEF', '\n'.join(logs.output))
        with override_settings(DISCORD_WEBHOOKS={'Testowy': 'http://example.com/'}):
            event, sender = self.delivered(self.event())
        self.assertEqual(event.status, 'failed')
        sender.assert_not_called()

    def test_error_after_commit_never_propagates(self):
        event = self.event()
        with patch('core.workflow_events.deliver', side_effect=RuntimeError), \
                self.assertLogs('core.workflow_events', 'ERROR'):
            notify_after_commit(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, 'unknown')
        # Nawet gdy nie da się zapisać wyniku, operacja workflow nie kończy się błędem 500.
        with patch('core.workflow_events.deliver', side_effect=RuntimeError), \
                patch.object(WorkflowEvent.objects, 'using', side_effect=RuntimeError), \
                self.assertLogs('core.workflow_events', 'ERROR') as logs:
            notify_after_commit(event.pk)
        self.assertIn('Could not record notification outcome', logs.output[-1])
