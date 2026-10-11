"""Lista zgłoszeń rekrutacyjnych i kolejka na pulpicie: rozpoznawanie kandydata z wiadomości, kolumny, wyszukiwanie, sortowanie, filtry i dostęp."""
from email.message import EmailMessage

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.admin import MailboxConnectionForm
from core.models import MailboxConnection, Recruitment
from core.recruitment_message import form_fields
from core.services.recruitment_decisions import set_all_decisions
from core.services.recruitment_samples import parse_sample
from people.models import Person, Role
from texts.models import Anthology, Review

from .support import FORM_BODY, RecruitmentTeamMixin


class RecruitmentMessageParsingTests(SimpleTestCase):
    def test_generated_template_identity_roles_and_original_content(self):
        msg = EmailMessage()
        msg['From'] = 'Formularz <noreply@example.test>'
        msg['Subject'] = 'Nowe zgłoszenie do Fantazmatów – Redakcja'
        msg.set_content(FORM_BODY)
        data = parse_sample(msg.as_bytes())
        self.assertEqual(data['applicant_name'], 'Maria Anna Nowak-Kowalska')
        self.assertEqual(data['email'], 'candidate@example.test')
        self.assertEqual(data['mail_roles'], ['audio_proofreaders', 'illustrators', 'typesetters'])
        self.assertEqual(data['mail_body'], FORM_BODY.strip())
        self.assertIn('noreply@example.test', data['mail_sender'])

    def test_html_template_keeps_links_and_unexpanded_placeholders_are_not_identity(self):
        msg = EmailMessage()
        msg['From'] = 'candidate@example.test'
        msg['Subject'] = 'Lektor'
        msg.set_content('<div>Nowe zgłoszenie do Fantazmatów</div><div>Imię i nazwisko: Jan Nowak</div>'
                        '<div>E-mail: jan@example.test</div><div>Wybrane role: Ilustracje</div><div>WIADOMOŚĆ</div>'
                        '<p><a href="https://example.test/show">Portfolio</a></p>', subtype='html')
        data = parse_sample(msg.as_bytes())
        self.assertEqual(data['applicant_name'], 'Jan Nowak')
        self.assertEqual(data['mail_roles'], ['illustrators'])
        self.assertIn('https://example.test/show', data['mail_body'])
        fields = form_fields(FORM_BODY.replace('Maria Anna Nowak-Kowalska', '[fz-name]')
                             .replace('Candidate@Example.test', '[fz-email]'))
        self.assertEqual(fields['name'], '')
        self.assertEqual(fields['email'], '')
        self.assertEqual(form_fields('Zwykła wiadomość\n' + FORM_BODY), {})


class RecruitmentListTests(RecruitmentTeamMixin, TestCase):
    def test_list_search_and_sort_use_candidate_not_form_sender(self):
        record = Recruitment.objects.create(applicant_name='Maria Anna Nowak-Kowalska', email='candidate@example.test')
        early = Recruitment.objects.create(applicant_name='Anna Nowak', mail_sender='Z nadawca')
        late = Recruitment.objects.create(first_name='Zyta', last_name='Nowak', mail_sender='A nadawca')
        self.client.force_login(self.coordinator)
        url = reverse('core:recruitment_list')
        response = self.client.get(url, {'sort': 'sender'})
        self.assertEqual([row.pk for row in response.context['items']], [early.pk, record.pk, late.pk])
        response = self.client.get(url, {'q': 'Maria Anna'})
        self.assertEqual([row.pk for row in response.context['items']], [record.pk])

    def test_subject_name_columns_and_notes_below_reason_persist(self):
        self.client.force_login(self.admin)
        record = Recruitment.objects.create(mail_subject='Nowe zgłoszenie do Fantazmatów – Jan Nowak – Redakcja, Korekta')
        doc = html.fromstring(self.client.get(reverse('core:recruitment_list'), {'sort': 'subject'}).content)
        self.assertEqual(doc.xpath('string(//td[@class="recruitment-subject"])'), 'Jan Nowak')
        headers = [th.text_content().strip() for th in doc.xpath('//table/thead/tr/th')]
        self.assertEqual(headers, ['E-mail', 'Kto', 'Rola', 'Data wiadomości', 'Przyjęty/Odrzucony', 'Powiadomiony'])
        self.assertEqual(len(doc.xpath('//table/tbody/tr[1]/td')), 6)
        self.assertEqual(doc.xpath('//td[@class="recruitment-subject"]/a/@href'),
                         [reverse('core:recruitment_detail', args=[record.pk])])
        empty = html.fromstring(self.client.get(reverse('core:recruitment_list'), {'q': 'nieistniejący-kandydat'}).content)
        self.assertEqual(empty.xpath('//table/tbody/tr/td/@colspan'), ['6'])
        url = reverse('core:recruitment_detail', args=[record.pk])
        page = self.client.get(url)
        doc = html.fromstring(page.content)
        self.assertEqual(doc.xpath('//main//textarea/@name'), ['other-decision_reason', 'other-unofficial_notes'])
        response = self.client.post(url, {'role': 'other', 'version': page.context['sections'][0]['version'],
                                          'other-status': 'accepted', 'other-decision_reason': 'Tak',
                                          'other-unofficial_notes': 'Prywatna uwaga'})
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.role_decisions.get(role='other').unofficial_notes, 'Prywatna uwaga')

    def test_author_menu_read_access_without_submission_or_write_access(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', email='author@example.test')
        for user in (self.coordinator, self.recruiter, self.admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('core:author_list'), {'accepted': '0', 'sort': 'email'}).status_code, 200)
            self.assertEqual(self.client.get(reverse('core:author_detail', args=[author.pk])).status_code, 200)
            self.assertContains(self.client.get(reverse('core:home')), reverse('core:author_list'))
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.get(reverse('core:review_bulk_import')).status_code, 403)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('core:author_list')).status_code, 403)
        self.assertEqual(self.client.get(reverse('core:author_detail', args=[author.pk])).status_code, 403)

    def test_only_one_active_recruitment_mailbox(self):
        first = MailboxConnection(name='Rekrutacja', purpose=MailboxConnection.Purpose.RECRUITMENT, host='imap.example.com',
                                  port=993, username='a@example.com', folder='INBOX', is_active=True)
        first.set_password('secret')
        first.save()
        form = MailboxConnectionForm(data={
            'name': 'Druga', 'purpose': MailboxConnection.Purpose.RECRUITMENT, 'host': 'imap.example.com',
            'port': 993, 'security': first.security, 'username': 'b@example.com', 'password': 'x', 'folder': 'INBOX',
            'recruitment_subjects': '', 'is_active': 'on'})
        self.assertFalse(form.is_valid())
        self.assertIn('tylko jedna skrzynka rekrutacji', str(form.errors))


class RecruitmentDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.admin = users.objects.create_superuser('queue-admin', 'admin@example.test', 'test')
        cls.coordinator = users.objects.create_user('queue-coord')
        cls.member = users.objects.create_user('queue-member')
        for user in (cls.coordinator, cls.member):
            person = Person.objects.create(user=user, first_name='Jan', last_name=user.username)
            if user == cls.coordinator:
                # Ogólna rola „Koordynator” (dawna) nadal daje dostęp do kolejki.
                person.roles.add(Role.objects.get_or_create(name='Koordynator')[0])
        cls.multi = Recruitment.objects.create(mail_subject='Kilka ról', mail_roles=['editors', 'reviewers', 'editors'])
        Recruitment.objects.create(first_name='Ręcznie', department='editors')
        Recruitment.objects.create(first_name='Dźwięk', department='sound')
        Recruitment.objects.create(mail_subject='Bez roli')
        Recruitment.objects.create(mail_roles=['editors'], status='accepted')
        Recruitment.objects.create(mail_roles=['editors'], status='rejected')
        anthology = Anthology.objects.create(title='Antologia kolejki')
        Review.objects.create(anthology=anthology, title='Jawny tytuł', length=1000, author_first_name='Tajny',
                              author_last_name='Autor', email='secret@example.test', status='accepted',
                              author_notified_at=timezone.now())
        Review.objects.create(anthology=anthology, title='Ukryty tytuł', length=1000, status='accepted',
                              author_notified_at=timezone.now(), is_hidden=True)

    def test_counts_and_links_match_pending_records_for_both_privileged_roles(self):
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            response = self.client.get(reverse('core:home'))
            counts = {row['role']: row['count'] for row in response.context['pending_recruitment']}
            self.assertEqual(counts, {'editors': 2, 'reviewers': 1, 'sound': 1, 'other': 1})
            doc = html.fromstring(response.content)
            for link in doc.xpath('//*[@data-pending-recruitment]//a'):
                page = self.client.get(link.get('href'))
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.context['page_obj'].paginator.count, int(link.text_content().rsplit(':', 1)[1]))
                self.assertTrue(all(item.status == 'new' for item in page.context['items']))
            self.assertContains(response, 'Jawny tytuł')
            if user == self.coordinator:
                self.assertNotContains(response, 'Tajny Autor')
                self.assertNotContains(response, 'Ukryty tytuł')

    def test_no_pending_hides_entire_card_and_member_cannot_access_queue(self):
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(reverse('core:home')), 'data-pending-recruitment')
        self.assertEqual(self.client.get(reverse('core:recruitment_list')).status_code, 403)
        with transaction.atomic():
            for record in Recruitment.objects.select_for_update().filter(status='new'):
                set_all_decisions(record, 'accepted')
        self.client.force_login(self.coordinator)
        self.assertNotContains(self.client.get(reverse('core:home')), 'data-pending-recruitment')

    def test_filters_preserved_with_search_and_pagination(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('core:recruitment_list'),
                                   {'role': 'editors', 'status': 'new', 'q': 'Kilka', 'page_size': 10})
        self.assertEqual([item.pk for item in response.context['items']], [self.multi.pk])
        self.assertEqual(response.context['role'], 'editors')
        self.assertEqual(response.context['status'], 'new')
