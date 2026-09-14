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
    def setUp(self):
        self.root=get_user_model().objects.create_superuser('root','root@example.com','test')
        self.user=get_user_model().objects.create_user('staff',is_staff=True)
        self.url=reverse('core:discord_test')
        self.client.force_login(self.root)
    def data(self,**updates):
        data={'channel':'Testowy','content':'Wiadomość próbna','token':self.client.get(self.url).context['form']['token'].value()}
        data.update(updates);return data
    def test_page_hides_secrets(self):
        response=self.client.get(self.url)
        self.assertContains(response,'Testowy');self.assertNotContains(response,'test-secret')
    def test_access_requires_superuser(self):
        for method in ('get','post'):
            self.client.force_login(self.user)
            self.assertEqual(getattr(self.client,method)(self.url).status_code,403)
        self.client.logout();self.assertEqual(self.client.get(self.url).status_code,302)
    @override_settings(DISCORD_WEBHOOKS={})
    def test_unconfigured(self):
        self.assertContains(self.client.get(self.url),'Nie skonfigurowano kanałów')
    @patch('core.views.discord.send_message',return_value='123456')
    def test_send_and_replay(self,send):
        data=self.data()
        self.assertEqual(self.client.post(self.url,data).status_code,302)
        self.assertEqual(self.client.post(self.url,data).status_code,302)
        send.assert_called_once_with(URL,'Wiadomość próbna')
        self.assertEqual(DiscordDispatch.objects.get().status,'sent')
    @patch('core.views.discord.send_message')
    def test_validation(self,send):
        for updates in ({'channel':'Inny'},{'content':' '},{'content':'a'*2001},{'token':'fake'}):
            self.assertEqual(self.client.post(self.url,self.data(**updates)).status_code,400)
        send.assert_not_called();self.assertFalse(DiscordDispatch.objects.exists())
    @patch('core.views.discord.send_message',side_effect=DeliveryError('Brak potwierdzenia',uncertain=True))
    def test_timeout_recorded_without_retry(self,send):
        data=self.data();self.client.post(self.url,data);self.client.post(self.url,data)
        self.assertEqual(DiscordDispatch.objects.get().status,'unknown');self.assertEqual(send.call_count,1)
    def test_csrf_required(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.root)
        self.assertEqual(client.post(self.url,self.data()).status_code,403)
    @override_settings(DISCORD_WEBHOOKS={'Test':'https://example.com/webhooks/123/secret'})
    def test_host_restricted_without_secret_in_error(self):
        with self.assertRaises(ConfigurationError):channels()
        response=self.client.get(self.url);self.assertNotContains(response,'secret')
    @patch('core.discord_webhook.build_opener')
    def test_transport_payload_and_confirmation(self,opener):
        response=MagicMock();response.status=200;response.read.return_value=b'{"id":"123456"}'
        opener.return_value.open.return_value.__enter__.return_value=response
        self.assertEqual(send_message(URL,'@everyone test'),'123456')
        request=opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url,URL+'?wait=true')
        self.assertEqual(json.loads(request.data)['allowed_mentions'],{'parse':[]})
        self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'],10)
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
