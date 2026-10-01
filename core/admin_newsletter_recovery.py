"""Temporary admin-only recovery. Remove the mixin registration to uninstall."""
import imaplib
import re
import ssl
from lxml.etree import ParserError
from email import policy
from email.parser import BytesParser

from cryptography.fernet import InvalidToken
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.template.response import TemplateResponse
from django.urls import path
from django.views.decorators.http import require_http_methods

from core.services.mailbox import MailboxError, encode_folder
from core.services.mailbox_import import default_mailbox, mailbox_key
from core.services.newsletters import parse_consents, record_consents
from core.services.review_import_parser import clean_pasted_submission, unbracket, mail_submission_rows
from core.services.mailbox_body import fetch_text_body
import time

MAX_BYTES = 15 * 1024 * 1024
SESSION_KEY = 'newsletter_recovery_cursor'
SALT = 'newsletter-recovery-v1'


def extract_consent(raw):
    """Only explicit checkbox values in one recognizable submission, never prose."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = message.get_body(preferencelist=('plain', 'html'))
    if body is None:
        return None
    text = body.get_content()
    if body.get_content_type() == 'text/html':
        from lxml import html
        root = html.fromstring(text)
        for node in root.xpath('//br | //p | //div | //tr'):
            node.tail = '\n' + (node.tail or '')
        text = root.text_content()
    # Old mail templates append this label directly after the checkbox field,
    # sometimes without a semicolon or even a newline.
    text = re.split(r'(?i)tre[śs][ćc]\s+wiadomo[śs]ci\s*:?', text, maxsplit=1)[0]
    text = clean_pasted_submission(text)
    text = re.sub(r';[ \t]*\r?\n[ \t]*', ';', text)
    candidates = []
    for kind, _, parts in mail_submission_rows(text):
        email, choices = (parts[5], parts[7]) if kind == 'new' else (parts[4], parts[7] if len(parts) > 7 else '')
        try:
            validate_email(email)
            consents = parse_consents(choices)
        except ValidationError:
            return None
        candidates.append((email, consents))
    return candidates[0] if len(candidates) == 1 else None


def recover_batch(config, state=None):
    """Read-only IMAP; bounded requests and a stable UID snapshot across batches."""
    client = None
    state = dict(state or {'last': 0, 'seen': 0, 'matched': 0, 'skipped': 0})
    if state.get('key', mailbox_key(config)) != mailbox_key(config):
        raise MailboxError('Zmieniono konfigurację skrzynki. Rozpocznij od początku.')
    try:
        context = ssl.create_default_context()
        if config.security == 'ssl':
            client = imaplib.IMAP4_SSL(config.host, config.port, ssl_context=context, timeout=15)
        elif config.security == 'starttls':
            client = imaplib.IMAP4(config.host, config.port, timeout=15)
            client.starttls(ssl_context=context)
        else:
            raise MailboxError('Nieobsługiwany sposób szyfrowania.')
        client.login(config.username, config.get_password())
        folder = '"' + encode_folder(config.folder).replace('\\', '\\\\').replace('"', '\\"') + '"'
        if client.select(folder, readonly=True)[0] != 'OK':
            raise MailboxError('Nie można otworzyć folderu skrzynki.')
        _, validity = client.response('UIDVALIDITY')
        if not validity or not validity[0]:
            raise MailboxError('Serwer nie podał UIDVALIDITY.')
        validity = int(validity[0])
        if 'validity' in state and state['validity'] != validity:
            raise MailboxError('Folder zmienił identyfikatory. Rozpocznij od początku.')
        # SEARCH includes read, unread and previously downloaded messages alike.
        if 'maximum' in state:
            if state['last'] >= state['maximum']:
                state['done'] = True
                return state
            status, data = client.uid('SEARCH', None, 'UID', f"{state['last'] + 1}:{state['maximum']}")
        else:
            status, data = client.uid('SEARCH', None, 'ALL')
        if status != 'OK':
            raise MailboxError('Nie udało się pobrać listy wiadomości.')
        uids = sorted(int(x) for x in (data[0] or b'').split())
        state.setdefault('maximum', max(uids, default=0))
        state.setdefault('total', len(uids))
        state.update(key=mailbox_key(config), validity=validity)
        remaining = [uid for uid in uids if state['last'] < uid <= state['maximum']]
        started = time.monotonic()
        processed = 0
        for uid in remaining[:10]:
            if processed and time.monotonic() - started > 10: break
            raw = fetch_text_body(client, uid)
            result = None
            if raw is not None:
                try:
                    result = extract_consent(raw)
                except (ValidationError, ValueError, TypeError, UnicodeError, LookupError, ParserError):
                    result = None
            else:
                state['oversized'] = state.get('oversized', 0) + 1
            if result is not None:
                email, consents = result
                record_consents(email, **consents)
                state['matched'] += 1
            else:
                state['skipped'] += 1
                state.setdefault('skipped_uids', []).append(uid)
                state['skipped_uids'] = state['skipped_uids'][-500:]
                state['skipped_uids_truncated'] = state['skipped'] > 500
            state['last'] = uid
            state['seen'] += 1
            processed += 1
        state['done'] = processed == len(remaining)
        return state
    except (imaplib.IMAP4.error, OSError, InvalidToken, ValueError) as error:
        raise MailboxError('Nie udało się odczytać skrzynki. Sprawdź ustawienia i ponów partię.') from None
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


class NewsletterRecoveryAdminMixin:
    change_list_template = 'admin/core/mailbox_recovery_link.html'

    def get_urls(self):
        return [path('odzyskaj-zgody/', self.admin_site.admin_view(
            require_http_methods(['GET', 'POST'])(self.recover_newsletters)),
            name='core_mailbox_recover_newsletters')] + super().get_urls()

    def recover_newsletters(self, request):
        if not request.user.is_active or not request.user.is_superuser:
            raise PermissionDenied
        if request.method == 'POST' and request.POST.get('restart') == '1':
            request.session.pop(SESSION_KEY, None)
            token = ''
        else:
            token = request.POST.get('cursor') or request.session.get(SESSION_KEY, '')
        state = None
        error = ''
        try:
            if token:
                state = signing.loads(token, salt=SALT, max_age=86400)
                if state['user'] != request.user.pk:
                    raise PermissionDenied
            if request.method == 'POST':
                state = recover_batch(default_mailbox(), state)
                state['user'] = request.user.pk
                state['retries'] = 0
                token = signing.dumps(state, salt=SALT, compress=True)
                request.session[SESSION_KEY] = token
        except signing.BadSignature:
            error = 'Sesja narzędzia wygasła. Otwórz stronę ponownie i rozpocznij od początku.'
            token = ''
            request.session.pop(SESSION_KEY, None)
        except MailboxError as exc:
            error = str(exc)
            if state:
                state['retries'] = state.get('retries', 0) + 1
                token = signing.dumps(state, salt=SALT, compress=True)
                request.session[SESSION_KEY] = token
        auto_retry = bool(error and state and state.get('retries', 0) <= 3)
        return TemplateResponse(request, 'admin/core/newsletter_recovery.html', {
            **self.admin_site.each_context(request), 'title': 'Odzyskaj zgody ze skrzynki Teksty',
            'state': state, 'cursor': token, 'error': error,
            'continue_run': bool(request.method == 'POST' and state and not state.get('done') and (not error or auto_retry)),
            'retry_delay': 15000 if error else 2000,
        })
