"""Załączniki wiadomości e-mail: treść listu nie jest załącznikiem, a nazwana część wieloczęściowa
(np. przekazana wersja alternatywna) trafia do załączników w całości jako .eml."""
from email import message_from_bytes, policy

from django.test import SimpleTestCase

from core.services.mail_attachments import attachments

MESSAGE = b'''MIME-Version: 1.0
Subject: Tekst do redakcji
Content-Type: multipart/mixed; boundary="B"

--B
Content-Type: multipart/alternative; boundary="A"

--A
Content-Type: text/plain; charset=utf-8

Tre\xc5\x9b\xc4\x87 listu
--A
Content-Type: text/html; charset=utf-8

<p>Tre\xc5\x9b\xc4\x87 listu</p>
--A--
--B
Content-Type: text/plain; charset=utf-8
Content-Disposition: inline; filename="notatka.txt"

Notatka
--B
Content-Type: multipart/alternative; boundary="C"
Content-Disposition: attachment

--C
Content-Type: text/plain

Wersja tekstowa
--C--
--B
Content-Type: application/octet-stream
Content-Transfer-Encoding: base64

AAEC
--B--
'''


class AttachmentTests(SimpleTestCase):
    def test_named_inline_files_and_multipart_attachments_are_saved_but_the_body_is_not(self):
        found = list(attachments(message_from_bytes(MESSAGE, policy=policy.default)))
        self.assertEqual([name for name, _ in found], ['notatka.txt', 'zalacznik-2.eml', 'zalacznik-3.bin'])
        self.assertEqual(found[0][1].strip(), b'Notatka')
        # Część wieloczęściowa zachowuje nagłówki i granice, więc da się ją otworzyć jako wiadomość.
        self.assertTrue(found[1][1].startswith(b'Content-Type: multipart/alternative; boundary="C"'))
        self.assertIn(b'Wersja tekstowa', found[1][1])
        self.assertEqual(found[2][1], b'\x00\x01\x02')
