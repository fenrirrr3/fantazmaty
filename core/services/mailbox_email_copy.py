from email.utils import getaddresses

from core.services.mailbox import MailboxError, read_headers


def sender_emails(config, validity, **filters):
    """All matching headers, using one stable IMAP UID cursor, without receipts."""
    addresses, cursor, seen_cursors = {}, None, set()
    while True:
        result = read_headers(config, cursor, **filters)
        if result['validity'] != validity:
            raise MailboxError('Folder pocztowy zmienił się. Pobierz nagłówki ponownie.')
        for row in result['rows']:
            for _, address in getaddresses([row['sender']]):
                if '@' in address:
                    addresses.setdefault(address.casefold(), address)
        cursor = result['next_cursor']
        if not cursor:
            return list(addresses.values())
        identity = (cursor['anchor'], cursor['boundary'])
        if identity in seen_cursors:
            raise MailboxError('Lista wiadomości zmieniła się podczas pobierania. Spróbuj ponownie.')
        seen_cursors.add(identity)
