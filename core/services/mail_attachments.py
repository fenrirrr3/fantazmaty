"""Read real MIME attachments, including named inline documents, without fetching links."""
import mimetypes

from core.services.mailbox import MailboxError


def attachments(message):
    counter = 0

    def visit(part):
        nonlocal counter
        name = part.get_filename()
        kind = part.get_content_type()
        disposition = part.get_content_disposition()
        attached_message = kind == 'message/rfc822'
        explicit = bool(name) or disposition == 'attachment' or attached_message
        if part.is_multipart() and not explicit:
            for child in part.iter_parts():
                yield from visit(child)
            return
        # The plain/HTML body is not an attachment. A filename or attachment
        # disposition wins, even for inline text or HTML files.
        if not explicit and kind in ('text/plain', 'text/html'):
            return
        counter += 1
        extension = '.eml' if attached_message or part.is_multipart() else mimetypes.guess_extension(kind) or '.bin'
        filename = str(name or f'zalacznik-{counter}{extension}')
        if attached_message:
            messages = part.get_payload()
            payload = b'\r\n'.join(item.as_bytes(policy=part.policy) for item in messages) if isinstance(messages, list) else part.get_payload(decode=True)
        elif part.is_multipart():
            payload = part.as_bytes(policy=part.policy)
        else:
            payload = part.get_payload(decode=True)
        if payload is None:
            raise MailboxError(f'Nie można odczytać załącznika „{filename}”. Niczego nie zapisano.')
        yield filename, payload

    yield from visit(message)
