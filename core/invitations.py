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


DISCORD_NO_LINK = ('Link zapraszający na Discorda nie jest ustawiony – ustaw go w panelu: '
                   '„Discord – ustawienia (zaproszenie)”.')
DISCORD_FIELD = '_invite_discord'


def discord_invite_url():
    """Link zapraszający na serwer Discorda z ustawień w panelu (pusty – brak)."""
    from core.notification_models import DiscordSettings
    return DiscordSettings.invite_url_value()


def setup_discord_field(field):
    """Pole „Zaproś też na Discorda”: domyślnie zaznaczone, gdy link jest ustawiony; bez linku – nieaktywne z podpowiedzią."""
    url = discord_invite_url()
    field.initial = bool(url)
    if url:
        field.help_text = f'Link {url} trafi do maila z zaproszeniem do CMS-u.'
    else:
        field.disabled = True
        field.help_text = DISCORD_NO_LINK


def discord_checkbox():
    """Pole wyboru obok przycisku „Wyślij zaproszenie” (formularz konta i osoby); bez linku – podpowiedź."""
    from django.utils.html import format_html
    url = discord_invite_url()
    if not url:
        return format_html('<div class="help cms-inline-help">{}</div>', DISCORD_NO_LINK)
    return format_html('<label class="cms-inline-help"><input type="checkbox" name="{}" value="1" checked> '
                       'Zaproś też na Discorda ({})</label>', DISCORD_FIELD, url)


def send_invitation(user, request, discord=False):
    """Wysyła zaproszenie (z linkiem na Discorda, gdy discord i link jest ustawiony); ValidationError, gdy konta
    nie da się zaprosić."""
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
        'discord_url': discord_invite_url() if discord else '',
    }
    subject = render_to_string('registration/invitation_subject.txt', context).strip()
    body = render_to_string('registration/invitation_email.txt', context)
    send_mail(subject, body, None, [user.email])


class InvitationConfirmView(PasswordResetConfirmView):
    token_generator = invitation_token_generator
    extra_context = {'invitation': True}
