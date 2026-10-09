"""Discord transport: only configured HTTPS webhooks, no redirects, clear outcomes."""
import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase, override_settings

from core.discord_webhook import ConfigurationError, DeliveryError, channels, send_message

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
