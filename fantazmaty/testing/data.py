"""Zestawy danych do testów list, tabel i rekrutacji."""
from email.message import EmailMessage

from django.contrib.auth import get_user_model

from authors.models import Author
from people import roles
from people.models import Person, Role


class CoreTestDataMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        cls.superuser = User.objects.create_superuser(
            username="superadmin", email="superadmin@example.com", password="test-password")
        for attr, role_name in (("reviewer", roles.REVIEWER), ("editor", roles.EDITOR)):
            user = User.objects.create_user(username=attr, email=f"{attr}@example.com")
            person = Person.objects.create(first_name=attr, last_name="Testowy", email=user.email, user=user)
            role, _ = Role.objects.get_or_create(name=role_name)
            person.roles.add(role)
            setattr(cls, attr, user)
            setattr(cls, attr + "_person", person)
        cls.author = Author.objects.create(first_name="Autor", last_name="Testowy", email="author@example.com")
        from texts.models import Anthology
        cls.anthology = Anthology.objects.create(title="Antologia testowa")


def sample(uid=12, html=False, attachment=False):
    msg = EmailMessage()
    msg['From'] = 'Kandydat <candidate@example.test>'
    msg['Subject'] = f'Redakcja, Korekta audiobooków – zgłoszenie {uid}'
    msg['Date'] = 'Thu, 8 Oct 2026 10:20:00 +0200'
    if html:
        msg.set_content('<html><body><p>Moje zgłoszenie</p><script>alert(1)</script><img src="https://remote.invalid/tracker">Drugi akapit</body></html>', subtype='html')
    else:
        msg.set_content(f'Pełna treść zgłoszenia {uid}.')
    if attachment:
        msg.add_attachment(b'sample-document', maintype='application', subtype='octet-stream', filename='../../próbka.docx')
    return {'uid': uid, 'raw': msg.as_bytes()}
