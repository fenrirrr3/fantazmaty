from io import BytesIO
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError
import json
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings, Client
from django.urls import reverse
from core.discord_webhook import channels, send_message, ConfigurationError, DeliveryError
from core.models import DiscordDispatch

URL='https://discord.com/api/webhooks/12345/test-secret'

@override_settings(DISCORD_WEBHOOKS={'Testowy':URL})
class DiscordTests(TestCase):
    def test_removed_page(self):
        self.assertEqual(self.client.get('/discord-test/').status_code, 404)

    @patch('core.discord_webhook.build_opener')
    def test_transport_payload_and_confirmation(self,opener):
        response=MagicMock();response.status=200;response.read.return_value=b'{"id":"123456"}'
        opener.return_value.open.return_value.__enter__.return_value=response
        self.assertEqual(send_message(URL,'@everyone test'),'123456')
        request=opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url,URL+'?wait=true')
        self.assertEqual(json.loads(request.data)['allowed_mentions'],{'parse':[]})
        self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'],3)
    @patch('core.discord_webhook.build_opener')
    def test_transport_errors_hide_url(self,opener):
        for code in (400,403,429,500,302):
            opener.return_value.open.side_effect=HTTPError(URL,code,'secret',{},BytesIO(b'secret'))
            with self.assertRaises(DeliveryError) as raised:send_message(URL,'Test')
            self.assertNotIn('test-secret',str(raised.exception))
            self.assertEqual(raised.exception.uncertain,code>=500)
    @patch('core.discord_webhook.build_opener')
    def test_network_failure_is_uncertain(self,opener):
        opener.return_value.open.side_effect=URLError(URL)
        with self.assertRaises(DeliveryError) as raised:send_message(URL,'Test')
        self.assertTrue(raised.exception.uncertain);self.assertNotIn(URL,str(raised.exception))
