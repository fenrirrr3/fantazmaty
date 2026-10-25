"""Zaproszenie do CMS-u: e-mail z linkiem do ustawienia hasła, ważnym dłużej niż zwykły reset hasła.

Link działa jednorazowo – unieważnia go ustawienie hasła albo pierwsze logowanie (jak reset hasła w Django).
"""
from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.contrib.auth.views import PasswordResetConfirmView
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.utils.encoding import force_bytes
from django.utils.http import base36_to_int, urlsafe_base64_encode


def invitation_days():
    return settings.ACCOUNT_INVITATION_DAYS


class InvitationTokenGenerator(PasswordResetTokenGenerator):
    key_salt = 'core.invitations.InvitationTokenGenerator'

    def check_token(self, user, token):
        if not (user and token):
            return False
        try:
            timestamp = base36_to_int(token.split('-')[0])
        except ValueError:
            return False
        if self._num_seconds(self._now()) - timestamp > invitation_days() * 24 * 3600:
            return False
        return any(constant_time_compare(self._make_token_with_timestamp(user, timestamp, secret), token)
                   for secret in [self.secret, *self.secret_fallbacks])


invitation_token_generator = InvitationTokenGenerator()


def send_invitation(user, request):
    """Wysyła zaproszenie; ValidationError, gdy konta nie da się zaprosić."""
    if not user.is_active:
        raise ValidationError('Konto jest nieaktywne – najpierw je włącz.')
    if not user.email:
        raise ValidationError('Konto nie ma adresu e-mail.')
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    path = reverse('account_invitation', args=[uid, invitation_token_generator.make_token(user)])
    person = getattr(user, 'person_profile', None)
    context = {
        'first_name': (getattr(person, 'first_name', '') or user.first_name).strip(),
        'email': user.email,
        'link': request.build_absolute_uri(path),
        'login_url': request.build_absolute_uri(reverse('login')),
        'days': invitation_days(),
    }
    subject = render_to_string('registration/invitation_subject.txt', context).strip()
    body = render_to_string('registration/invitation_email.txt', context)
    send_mail(subject, body, None, [user.email])


class InvitationConfirmView(PasswordResetConfirmView):
    token_generator = invitation_token_generator
    extra_context = {'invitation': True}
