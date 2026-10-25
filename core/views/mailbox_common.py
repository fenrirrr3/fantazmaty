"""Wspólne elementy stron skrzynek (zgłoszenia tekstów i próbki rekrutacyjne).

Nagłówki zapamiętane w sesji i nocna migawka, podpisane tokeny wyboru, podgląd treści wiadomości,
nadawcy w komunikatach i kopiowanie adresów. Sole i klucze sesji podaje każda strona."""
import re

from django.core import signing
from django.http import JsonResponse

from core.mailbox_date import DEFAULT_SENT_SINCE
from core.services.mailbox import MailboxError
from core.services.mailbox_snapshots import automatic_notice, newer_snapshot

PREVIEW_LIMIT = 100000
TOKEN_AGE = 3600


def stored_headers(request, session_key, config, key, **defaults):
    """Nagłówki z sesji tego użytkownika i tej skrzynki (inne są usuwane), a przy GET – nowsza nocna migawka.

    Migawka trafia do sesji z domyślnymi filtrami `defaults`. Zwraca (nagłówki albo None, kontekst informacji
    o nocnym pobraniu)."""
    saved = request.session.get(session_key) or None
    if saved and ('sent_since' not in saved or saved.get('mailbox') != key or saved.get('user') != request.user.pk):
        request.session.pop(session_key, None)
        saved = None
    if request.method != 'GET':
        return saved, {}
    snapshot = newer_snapshot(config, key, request.user, saved)
    if snapshot is not None and not snapshot.error:
        saved = request.session[session_key] = {
            'user': request.user.pk, 'mailbox': key, 'sent_since': snapshot.sent_since, 'result': snapshot.result,
            'date_filter': {'date_filter_enabled': bool(snapshot.sent_since),
                            'sent_since': snapshot.sent_since or DEFAULT_SENT_SINCE.isoformat()},
            'fetched_at': snapshot.fetched_at.isoformat(), 'automatic': True, **defaults}
    return saved, automatic_notice(saved, snapshot)


def signed(payload, salt):
    """Podpisany słownik do formularza (lista wiadomości, kursor, podgląd)."""
    return signing.dumps(payload, salt=salt, compress=True)


def load_signed(token, salt, identity, expired, changed=None):
    """Słownik podpisany przez `signed` najwyżej godzinę temu i zgodny z `identity` (brak klucza – '').

    Zły lub stary podpis – MailboxError(`expired`), inna tożsamość – MailboxError(`changed` albo `expired`)."""
    try:
        payload = signing.loads(token, salt=salt, max_age=TOKEN_AGE)
    except signing.BadSignature:
        raise MailboxError(expired) from None
    if not isinstance(payload, dict) or any(payload.get(name, '') != value for name, value in identity.items()):
        raise MailboxError(changed or expired)
    return payload


def preview_data(sample):
    """Podgląd wiadomości z pól parse_sample: temat, nadawca i sam tekst (najwyżej PREVIEW_LIMIT znaków)."""
    body = sample['mail_body']
    return {'subject': sample['mail_subject'], 'sender': sample['mail_sender'],
            'body': body[:PREVIEW_LIMIT], 'truncated': len(body) > PREVIEW_LIMIT}


def with_senders(text, rows):
    """„Wiadomość N” w komunikacie zamienione na nadawcę z listy nagłówków (spoza listy – „Zgłoszenie”)."""
    senders = {row['uid']: row.get('sender') or 'Nieznana osoba' for row in rows}
    return re.sub(r'\bWiadomość\s+(\d+)', lambda match: senders.get(int(match[1]), 'Zgłoszenie'), text)


def copied_emails(config, validity, **filters):
    """Odpowiedź JSON z adresami nadawców wszystkich wiadomości pasujących do filtrów (bez znaczników pobrania)."""
    from core.services.mailbox_email_copy import sender_emails
    return JsonResponse({'emails': sender_emails(config, validity, **filters)})
