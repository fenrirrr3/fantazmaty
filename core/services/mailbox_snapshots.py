"""Nocne pobieranie nagłówków skrzynek (zadanie PythonAnywhere) i migawki pokazywane na ekranach pobierania."""
import logging

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.mailbox_date import DEFAULT_SENT_SINCE
from core.models import MailboxConnection, MailboxHeaderSnapshot, RecruitmentMailSource
from core.services.mailbox import MailboxError, active_mailbox, read_headers
from core.services.mailbox_import import default_mailbox, downloaded_uids, mailbox_key, receipts

logger = logging.getLogger(__name__)
UNEXPECTED_ERROR = 'Nieoczekiwany błąd podczas pobierania nagłówków. Szczegóły są w logu zadania.'


def recruitment_connection():
    return active_mailbox(MailboxConnection.Purpose.RECRUITMENT,
                          'Ustaw w panelu administratora jedną aktywną skrzynkę z przeznaczeniem „Rekrutacja do zespołu”.')


def imported_uids(config, validity):
    return set(receipts(config, validity).values_list('uid', flat=True)) | set(
        RecruitmentMailSource.objects.filter(mailbox_key=mailbox_key(config), uid_validity=validity).values_list('uid', flat=True))


def _submission_headers(config, sent_since):
    """Jak domyślny przycisk na ekranie zgłoszeń: bez naboru, bez już pobranych."""
    return read_headers(config, excluded=downloaded_uids(config), sent_since=sent_since)


def _recruitment_headers(config, sent_since):
    """Jak domyślny przycisk na ekranie próbek: wszystkie role, bez pobranych i dodanych do bazy."""
    return read_headers(config, excluded=lambda validity: imported_uids(config, validity),
                        recruitment_roles=[], sent_since=sent_since)


MAILBOXES = (('Zgłoszenia', default_mailbox, _submission_headers),
             ('Rekrutacja', recruitment_connection, _recruitment_headers))


def refresh_snapshots():
    """Pobiera pierwszą stronę nagłówków obu skrzynek i zapisuje migawki.

    Zwraca listę {'label', 'status' ('ok' | 'error' | 'skipped'), 'count', 'message'}.
    Błąd jednej skrzynki nie przerywa drugiej.
    """
    sent_since = DEFAULT_SENT_SINCE.isoformat()
    summary = []
    for label, connection, fetch in MAILBOXES:
        try:
            config = connection()
        except MailboxError as error:
            summary.append({'label': label, 'status': 'skipped', 'count': 0, 'message': str(error)})
            continue
        result, error = {}, ''
        try:
            result = fetch(config, sent_since)
        except MailboxError as failure:
            error = str(failure)
        except Exception:
            # Treść wyjątku może zawierać dane połączenia – do migawki trafia tylko ogólny komunikat.
            logger.exception('Nocne pobieranie nagłówków skrzynki %s nie powiodło się.', config.pk)
            error = UNEXPECTED_ERROR
        MailboxHeaderSnapshot.objects.update_or_create(mailbox=config, defaults={
            'mailbox_key': mailbox_key(config), 'sent_since': sent_since, 'result': result,
            'error': error, 'fetched_at': timezone.now()})
        summary.append({'label': label, 'status': 'error' if error else 'ok',
                        'count': len(result.get('rows', [])), 'message': error})
    return summary


def newer_snapshot(config, key, user, saved):
    """Migawka tej skrzynki nowsza niż nagłówki zapisane w sesji użytkownika (albo None).

    Nagłówki z sesji bez znacznika czasu lub z innej skrzynki traktujemy jak starsze.
    """
    snapshot = MailboxHeaderSnapshot.objects.filter(mailbox=config, mailbox_key=key).first()
    if snapshot is None:
        return None
    if saved and saved.get('user') == user.pk and saved.get('mailbox') == key and 'sent_since' in saved:
        stamp = parse_datetime(saved.get('fetched_at') or '')
        if stamp and stamp >= snapshot.fetched_at:
            return None
    return snapshot


def automatic_notice(saved, snapshot):
    """Kontekst informacji o nocnym pobraniu: czas migawki pokazanej z sesji i ewentualny błąd nowszej migawki."""
    notice = {}
    if saved and saved.get('automatic'):
        notice['automatic_fetch'] = parse_datetime(saved.get('fetched_at') or '')
    if snapshot is not None and snapshot.error:
        notice.update(automatic_error=snapshot.error, automatic_error_at=snapshot.fetched_at)
    return notice
