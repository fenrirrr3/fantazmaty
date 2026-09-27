"""Encrypt mailbox credentials at rest using the deployment's existing secret."""
import base64
import hashlib
from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


def _cipher():
    secrets = [settings.SECRET_KEY, *getattr(settings, 'SECRET_KEY_FALLBACKS', [])]
    return MultiFernet([Fernet(base64.urlsafe_b64encode(
        hashlib.sha256(('fantazmaty:mailbox:v1:' + secret).encode()).digest()
    )) for secret in secrets])


def encrypt_password(value):
    return _cipher().encrypt(value.encode()).decode()


def decrypt_password(value):
    return _cipher().decrypt(value.encode()).decode()
