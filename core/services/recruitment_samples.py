"""Recruitment mail storage without guessing the future candidate form layout."""
import hashlib
import re
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone
from lxml import html, etree

from core.models import Recruitment, RecruitmentMailSource, MailboxConnection, MailboxDownload
from core.recruitment_roles import ROLE_CHOICES
from core.services.mailbox import MailboxError
from core.services.mailbox_import import mailbox_key

MAX_SAMPLES = 50


def plain_body(message):
    part = message.get_body(preferencelist=('plain', 'html'))
    if part is None:
        return ''
    try:
        value = part.get_content()
    except (LookupError, UnicodeError):
        value = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
    if not isinstance(value, str):
        return ''
    if part.get_content_type() == 'text/html':
        try:
            tree = html.fromstring(value.encode('utf-8'), parser=html.HTMLParser(no_network=True, encoding='utf-8'))
            for node in tree.xpath('//script|//style|//head|//iframe|//object'):
                if node is tree:
                    return ''
                node.drop_tree()
            for node in tree.xpath('//a[@href]'):
                href = node.get('href', '')
                if href.startswith(('https://', 'http://', 'mailto:')):
                    node.tail = f' ({href})' + (node.tail or '')
            for node in tree.xpath('//br|//p|//div|//li|//tr'):
                node.tail = '\n' + (node.tail or '')
            value = tree.text_content()
        except (ValueError, etree.ParserError):
            value = ''
    return value.replace('\x00', '').strip()


def parse_sample(raw):
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        sender = str(message.get('Reply-To') or message.get('From') or '')[:2000]
        subject = str(message.get('Subject') or '(bez tematu)')[:2000]
        address = parseaddr(sender)[1].strip().lower()
        try:
            validate_email(address)
            if len(address) > 254:
                address = ''
        except ValidationError:
            address = ''
        try:
            received = parsedate_to_datetime(str(message.get('Date') or ''))
            if timezone.is_naive(received):
                received = timezone.make_aware(received)
            if not 1900 <= received.year <= 9998:
                received = None
        except (ValueError, TypeError, OverflowError):
            received = None
        # Longer names must not accidentally add the general Korekta role.
        remaining, matched = subject.casefold(), set()
        for key, label in sorted(ROLE_CHOICES, key=lambda choice: -len(choice[1])):
            if label.casefold() in remaining:
                matched.add(key)
                remaining = remaining.replace(label.casefold(), ' ')
        return dict(mail_subject=subject, mail_sender=sender, email=address,
            mail_body=plain_body(message), mail_received_at=received,
            mail_roles=[key for key, _ in ROLE_CHOICES if key in matched],
            mail_fingerprint=hashlib.sha256(raw).hexdigest())
    except (ValueError, TypeError, AttributeError, RecursionError) as error:
        raise MailboxError('Nie udało się odczytać struktury wiadomości. Niczego nie zapisano.') from error


def store_samples(config, validity, messages, *, downloaded=False):
    """Caller holds one transaction: all selected records succeed or all roll back."""
    key = mailbox_key(config)
    current = MailboxConnection.objects.select_for_update().filter(pk=config.pk).first()
    if current is None or not current.is_active or current.purpose != MailboxConnection.Purpose.RECRUITMENT or mailbox_key(current) != key:
        raise MailboxError('Zmieniono ustawienia skrzynki. Pobierz nagłówki ponownie.')
    records = []
    for row in messages:
        data = parse_sample(row['raw'])
        source = RecruitmentMailSource.objects.select_for_update().filter(mailbox_key=key, uid_validity=validity, uid=row['uid']).first()
        if source:
            record = source.recruitment
            if record.mail_fingerprint != data['mail_fingerprint']:
                raise MailboxError('Zawartość zapisanej wiadomości zmieniła się. Nie nadpisano zgłoszenia.')
        else:
            fingerprint = data.pop('mail_fingerprint')
            if data['mail_received_at']:
                data['submitted_at'] = timezone.localdate(data['mail_received_at'])
            record, _ = Recruitment.objects.get_or_create(mail_fingerprint=fingerprint, defaults=data)
            source = RecruitmentMailSource.objects.create(recruitment=record, mailbox_key=key, uid_validity=validity, uid=row['uid'])
        if downloaded:
            source.downloaded_at = timezone.now()
            source.save(update_fields=['downloaded_at'])
            MailboxDownload.objects.update_or_create(mailbox_key=key, uid_validity=validity, uid=row['uid'], defaults={})
        records.append(record)
    return records


def attachment_archive(messages):
    stream = BytesIO()
    try:
        with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
            for row in messages:
                msg = BytesParser(policy=policy.default).parsebytes(row['raw'])
                count = 0
                for part in msg.walk():
                    if part.is_multipart() or part.get_content_disposition() == 'inline':
                        continue
                    if not part.get_filename() and part.get_content_disposition() != 'attachment':
                        continue
                    filename = re.split(r'[/\\]', str(part.get_filename() or 'zalacznik.bin'))[-1]
                    filename = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '_', filename).strip(' .')[:180] or 'zalacznik.bin'
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        raise MailboxError('Nie można odczytać jednego z załączników. Niczego nie zapisano.')
                    count += 1
                    archive.writestr(f'wiadomosc-{row["uid"]}/{count:02d}-{filename}', payload)
                if not count:
                    archive.writestr(f'wiadomosc-{row["uid"]}/brak-zalacznikow.txt', 'Ta wiadomość nie zawiera załączników. Zgłoszenie zapisano w Rekrutacji.\n')
        stream.seek(0)
        return stream
    except Exception:
        stream.close()
        raise
