"""Pomocniki testów poczty, przyjmowania tekstów i rekrutacji (wiadomości, tokeny wyboru, atrapy IMAP)."""
from copy import deepcopy
from email.message import EmailMessage
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.urls import reverse
from docx import Document

from core.models import MailboxConnection
from core.services.mailbox_import import mailbox_key
from core.tests.security.support import TemporaryActivitySpoolMixin
from core.views.recruitment_mailbox import SALT as RECRUITMENT_SALT
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology

NAMED_BODY = '''Imię: Anna Maria
Nazwisko: Nowak-Kowalska
Pseudonim: A. M. Nowak
Tytuł opowiadania: [Opowieść; część druga]
Gatunek: Fantasy
Ostrzeżenia o treści: PRZEMOC; ŚMIERĆ
Liczba znaków ze spacjami: 37 930
Adres e-mail: ANNA@example.com
Numer telefonu:
Newsletter: premierach, naborach

Wiadomość do redakcji:
Dzień dobry; przesyłam tekst.
Adres e-mail: to jest fragment wiadomości, nie zmiana adresu.
"Dziękuję".'''

NAMED_MESSAGE = NAMED_BODY.split('Wiadomość do redakcji:\n')[1]

SUBMISSION_FIELDS = ('Jan Autor', 'Opowieść', 'fantasy', 'PRZEMOC', '1234', 'jan@example.com', '123456789',
                     'premierach, naborach', 'Prywatne; zdanie.\nDrugi wiersz.')
"""Pola zgłoszenia w nowym formacie CSV (z ostrzeżeniami, zgodami i wiadomością autora)."""

LEGACY_EXAMPLE = ('Renata Ościak;Dwadzieścia rys na amulecie;Grimdark [fantasy;37930;renata.osciak@gmail.com]'
                  '(mailto:fantasy%3B37930%3Brenata.osciak@gmail.com);727908378;Na pokład, psubraty;'
                  'premierach, naborach\\\n&#x20;&#x20;')
"""Rekord wklejony z edytora Markdown (link mailto, ucieczki), dokładnie jak w zgłoszonym błędzie."""

FORM_BODY = '''Nowe zgłoszenie do Fantazmatów

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
"""Treść wiadomości wygenerowanej przez formularz rekrutacyjny strony."""

CSV_LINE = 'Julia Moskalik;Potworna Przystan;dark fantasy;15204;girl@example.com;885711216;Na pokład, psubraty'


def named_mail(body=NAMED_BODY, *, html=False, subject='Nabór: „Test” – Opowieść', reply_to=None):
    """Wiadomość z formularza CF7 (pola z etykietami) z jednym załącznikiem."""
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = 'sender@example.com'
    if reply_to:
        message['Reply-To'] = reply_to
    message.set_content(body, subtype='html' if html else 'plain')
    message.add_attachment(b'original', maintype='application', subtype='octet-stream', filename='tekst.docx')
    return message.as_bytes()


def csv_mail(line=CSV_LINE):
    """Wiadomość w dawnym formacie CSV z prawdziwym plikiem DOCX w załączniku."""
    msg = EmailMessage()
    msg['From'] = 'other@example.com'
    msg['Reply-To'] = 'reply@example.com'
    msg['Subject'] = 'Julia Moskalik – Potworna Przystan'
    msg['Date'] = 'Tue, 12 May 2026 10:30:00 +0200'
    msg.set_content(line + '\nTa treść nie może trafić do zgłoszenia.')
    doc = Document()
    doc.add_paragraph('Koniec. następne zdanie.')
    stream = BytesIO()
    doc.save(stream)
    msg.add_attachment(stream.getvalue(), maintype='application',
                       subtype='vnd.openxmlformats-officedocument.wordprocessingml.document', filename='tekst.docx')
    return msg.as_bytes()


def identity_mail(body='nieobsługiwany stary formularz', received=None):
    """Wiadomość ze stałym Message-ID do testów tożsamości (odcisku) wiadomości."""
    m = EmailMessage()
    m['From'] = 'a@example.test'
    m['Subject'] = 'Autor – Tytuł'
    m['Message-ID'] = '<stable@example.test>'
    if received:
        m['Received'] = received
    m.set_content(body)
    m.add_attachment(b'original', maintype='application', subtype='octet-stream', filename='tekst.docx')
    return m.as_bytes()


def submission_selection(user, box, uids, validity=7):
    """Podpisany wybór wiadomości z ekranu pobierania zgłoszeń tekstów."""
    return signing.dumps({'user': user.pk, 'mailbox': mailbox_key(box), 'validity': validity, 'uids': list(uids)},
                         salt='mailbox-selection')


def fake_imap(imap, exists=b'1', validity=b'7'):
    """Konfiguruje atrapę imaplib.IMAP4_SSL: wybór folderu i UIDVALIDITY."""
    client = imap.return_value
    client.select.return_value = ('OK', [exists])
    client.response.return_value = ('UIDVALIDITY', [validity])
    return client


def sized_fetch(raw):
    """Odpowiedzi IMAP UID FETCH: najpierw rozmiar, potem treść tej samej wiadomości dla każdego UID."""
    def fetch(command, uid, fields):
        if 'RFC822.SIZE' in fields:
            return 'OK', [f'1 (UID {uid} RFC822.SIZE {len(raw)})'.encode()]
        return 'OK', [(f'1 (UID {uid} BODY[] {{{len(raw)}}})'.encode(), raw)]
    return fetch


class RecruitmentMailboxMixin:
    """Ekran próbek rekrutacyjnych z jedną wiadomością (UID 12) na liście nagłówków."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = get_user_model().objects.create_superuser('samples-admin', 'samples@example.test', 'test-only')
        cls.box = MailboxConnection.objects.create(name='rekrutacja', purpose='recruitment', host='imap.example.test',
                                                   username='samples@example.test', encrypted_password='unused')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse('core:recruitment_mailbox')
        patcher = patch('core.views.recruitment_mailbox.read_headers', return_value=dict(
            validity=7, rows=[dict(uid=12, sender='candidate@example.test', subject='Redakcja', date='')],
            total=1, next_cursor=None, previous_cursor=None))
        self.headers = patcher.start()
        self.addCleanup(patcher.stop)
        self.client.get(self.url)

    def payload(self, action='add', uids=(12,), **extra):
        token = signing.dumps(dict(user=self.user.pk, mailbox=mailbox_key(self.box), roles=[], show=False,
                                   sent_since='2026-10-02', kind='selection', validity=7, uids=list(uids)),
                              salt=RECRUITMENT_SALT)
        return {'action': action, 'date_filter_enabled': 'on', 'sent_since': '2026-10-02', 'roles': ['all'],
                'selection': token, 'selected': list(uids), **extra}


SUBMISSION_HEADERS = {
    'validity': 7, 'total': 51,
    'rows': [{'uid': 12, 'sender': 'anna@example.com', 'date': '1 października 2026, 15:00', 'subject': 'Opowieść',
              'message_id': '<test@example.com>'}],
    'next_cursor': {'anchor': 100, 'validity': 7, 'direction': 'older', 'boundary': 12}, 'previous_cursor': None}


class SubmissionMailboxMixin(TemporaryActivitySpoolMixin):
    """Ekran pobierania zgłoszeń tekstów: superużytkownik, skrzynka „teksty” i antologie w przygotowaniu."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = create_superuser('admin', 'admin@example.com', 'password')
        cls.box = MailboxConnection.objects.create(name='teksty', host='imap.example.com', username='teksty@example.com',
                                                   encrypted_password='unused', recruitment_subjects='Test')
        cls.book = Anthology.objects.create(title='Na pokład, psubraty', status=Anthology.Status.IN_PREPARATION)
        Anthology.objects.create(title='Test')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse('core:review_bulk_import')
        self.headers = deepcopy(SUBMISSION_HEADERS)

    def payload(self, uids=(12,)):
        """Pobranie wiadomości wskazanych podpisanym wyborem (bez wcześniejszego odczytu nagłówków)."""
        return {'action': 'download', 'selection': submission_selection(self.user, self.box, uids),
                'uids': [str(uid) for uid in uids]}

    def fetch_headers(self, **options):
        """Odczyt nagłówków (atrapą) i powrót na stronę, która pokazuje je z sesji."""
        with patch('core.views.mailbox.read_headers', return_value=deepcopy(self.headers)) as read:
            response = self.client.post(self.url, {'action': 'headers', **options})
            self.assertRedirects(response, self.url, fetch_redirect_response=False)
            self.assertEqual(read.call_count, 1)
        return self.client.get(self.url)


class RecruitmentTeamMixin:
    """Superużytkownik i konta zespołu z rolami istotnymi dla rekrutacji."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser('recruitment-admin', 'admin@example.test')
        cls.coordinator = create_member('coordinator', 'Koordynator redakcji')
        cls.recruiter = create_member('recruiter', 'Koordynator rekrutacji')
        cls.member = create_member('member', 'Redaktor')
