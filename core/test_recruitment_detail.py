from email.message import EmailMessage
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client, SimpleTestCase
from django.urls import reverse
from lxml import html

from core.models import Recruitment
from core.recruitment_message import form_fields
from core.services.recruitment_samples import parse_sample
from people.models import Person, Role


BODY = '''Nowe zgłoszenie do Fantazmatów

Imię i nazwisko: Maria Anna Nowak-Kowalska
E-mail: Candidate@Example.test
Wybrane role: Korekta audiobooków, Ilustracje, Skład i e-booki

WIADOMOŚĆ
Moja wiadomość: https://example.test/probka?x=1&y=2
<script>alert(1)</script>
javascript:alert(2)

PORTFOLIO ILUSTRATORA
https://example.test/portfolio

Pizza (odpowiedź opcjonalna): margherita
Potwierdzenie współpracy zdalnej i bez wynagrodzenia: Tak
Strona formularza: https://example.test/rekrutacja/
Data: 2026-10-08, 12:00
'''


class RecruitmentFormParsingTests(SimpleTestCase):
    def test_generated_template_identity_roles_and_original_content(self):
        msg = EmailMessage()
        msg['From'] = 'Formularz <noreply@example.test>'
        msg['Subject'] = 'Nowe zgłoszenie do Fantazmatów – Redakcja'
        msg.set_content(BODY)
        data = parse_sample(msg.as_bytes())
        self.assertEqual(data['applicant_name'], 'Maria Anna Nowak-Kowalska')
        self.assertEqual(data['email'], 'candidate@example.test')
        self.assertEqual(data['mail_roles'], ['audio_proofreaders', 'illustrators', 'typesetters'])
        self.assertEqual(data['mail_body'], BODY.strip())
        self.assertIn('noreply@example.test', data['mail_sender'])

    def test_html_template_keeps_links_and_unexpanded_placeholders_are_not_identity(self):
        msg = EmailMessage(); msg['From'] = 'candidate@example.test'; msg['Subject'] = 'Lektor'
        msg.set_content('<div>Nowe zgłoszenie do Fantazmatów</div><div>Imię i nazwisko: Jan Nowak</div><div>E-mail: jan@example.test</div><div>Wybrane role: Ilustracje</div><div>WIADOMOŚĆ</div><p><a href="https://example.test/show">Portfolio</a></p>', subtype='html')
        data = parse_sample(msg.as_bytes())
        self.assertEqual(data['applicant_name'], 'Jan Nowak')
        self.assertEqual(data['mail_roles'], ['illustrators'])
        self.assertIn('https://example.test/show', data['mail_body'])
        fields = form_fields(BODY.replace('Maria Anna Nowak-Kowalska', '[fz-name]').replace('Candidate@Example.test', '[fz-email]'))
        self.assertEqual(fields['name'], ''); self.assertEqual(fields['email'], '')
        self.assertEqual(form_fields('Zwykła wiadomość\n' + BODY), {})


class RecruitmentDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.admin = users.objects.create_superuser('detail-admin', 'admin@example.test', 'test')
        cls.coordinator = users.objects.create_user('detail-coordinator')
        cls.member = users.objects.create_user('detail-member', is_staff=True)
        for user in (cls.coordinator, cls.member):
            person = Person.objects.create(user=user, first_name='Jan', last_name=user.username)
            if user == cls.coordinator:
                person.roles.add(Role.objects.get_or_create(name='Koordynator redakcji')[0])
        cls.record = Recruitment.objects.create(applicant_name='Maria Anna Nowak-Kowalska',
            email='candidate@example.test', mail_body=BODY, mail_subject='Nowe zgłoszenie',
            mail_roles=['illustrators'], notes='Uwagi istniejące', notified=True)

    def setUp(self):
        self.url = reverse('core:recruitment_detail', args=[self.record.pk])

    def test_detail_links_safe_body_and_theme_buttons_together(self):
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            page = self.client.get(reverse('core:recruitment_list'))
            self.assertContains(page, self.url)
            response = self.client.get(self.url)
            self.assertContains(response, 'Maria Anna Nowak-Kowalska')
            self.assertContains(response, 'Uwagi istniejące')
            doc = html.fromstring(response.content)
            links = doc.xpath('//div[contains(@class,"recruitment-message")]//a/@href')
            self.assertIn('https://example.test/portfolio', links)
            self.assertIn('https://example.test/probka?x=1&y=2', links)
            self.assertFalse(any(link.startswith('javascript:') for link in links))
            self.assertFalse(doc.xpath('//div[contains(@class,"recruitment-message")]//script'))
            self.assertEqual(len(doc.xpath('//div[contains(@class,"theme-buttons")]/button')), 2)

    def test_privileged_decision_and_reason_only_no_notification_or_metadata_overwrite(self):
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            version = self.client.get(self.url).context['version']
            previous_date = Recruitment.objects.get(pk=self.record.pk).notified_at
            with patch('django.core.mail.send_mail') as send:
                response = self.client.post(self.url, {'version': version, 'status': 'rejected',
                    'decision_reason': 'Uzasadnienie decyzji', 'notified': '', 'mail_body': 'Zmiana', 'email': 'other@example.test'})
                self.assertRedirects(response, self.url)
                send.assert_not_called()
            record = Recruitment.objects.get(pk=self.record.pk)
            self.assertEqual(record.status, 'rejected'); self.assertEqual(record.decision_reason, 'Uzasadnienie decyzji')
            self.assertEqual(record.mail_body, BODY); self.assertEqual(record.email, 'candidate@example.test')
            self.assertTrue(record.notified); self.assertEqual(record.notified_at, previous_date)
            self.assertEqual(record.notes, 'Uwagi istniejące')

    def test_no_decision_invalid_status_and_stale_edit(self):
        self.client.force_login(self.coordinator)
        version = self.client.get(self.url).context['version']
        self.assertEqual(self.client.post(self.url, {'version': version, 'status': 'bad'}).status_code, 400)
        record = Recruitment.objects.get(pk=self.record.pk); record.status = 'accepted'; record.save()
        response = self.client.post(self.url, {'version': version, 'status': 'rejected', 'decision_reason': 'Nie zgub szkicu'})
        self.assertContains(response, 'Nie zgub szkicu', status_code=409)
        record.refresh_from_db(); self.assertEqual(record.status, 'accepted')
        version = self.client.get(self.url).context['version']
        self.assertEqual(self.client.post(self.url, {'version': version, 'status': 'new'}).status_code, 302)
        record.refresh_from_db(); self.assertIsNone(record.accepted)

    def test_permissions_and_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'status': 'accepted'}).status_code, 403)
        client = Client(enforce_csrf_checks=True); client.force_login(self.coordinator)
        self.assertEqual(client.post(self.url, {'status': 'accepted'}).status_code, 403)

    def test_list_search_and_sort_use_candidate_not_form_sender(self):
        self.client.force_login(self.coordinator)
        early = Recruitment.objects.create(applicant_name='Anna Nowak', mail_sender='Z nadawca')
        late = Recruitment.objects.create(first_name='Zyta', last_name='Nowak', mail_sender='A nadawca')
        url = reverse('core:recruitment_list')
        response = self.client.get(url, {'sort': 'sender'})
        self.assertEqual([row.pk for row in response.context['items']], [early.pk, self.record.pk, late.pk])
        response = self.client.get(url, {'q': 'Maria Anna'})
        self.assertEqual([row.pk for row in response.context['items']], [self.record.pk])
