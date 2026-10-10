"""Database-backed limits shared by all web workers; no account-wide lockout."""
from datetime import timedelta
from math import ceil
from ipaddress import ip_address

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.deprecation import MiddlewareMixin

from core.models import AuthenticationAttempt

# Failed attempts per account per hour, summed over all client addresses.
ACCOUNT_LOGIN_LIMIT = 100
ACCOUNT_RESET_LIMIT = 20


def consume(key, limit, seconds, now):
    with transaction.atomic():
        record, _ = AuthenticationAttempt.objects.select_for_update().get_or_create(
            key=key, defaults={'expires_at': now + timedelta(seconds=seconds)})
        if record.expires_at <= now:
            record.attempts = 0
            record.expires_at = now + timedelta(seconds=seconds)
        if record.attempts >= limit:
            return max(1, ceil((record.expires_at - now).total_seconds()))
        record.attempts += 1
        record.save(update_fields=['attempts', 'expires_at'])
    return 0


class AuthenticationThrottleMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        if request.method != 'POST' or not match or match.url_name not in ('login', 'password_reset'):
            return None
        if not getattr(settings, 'AUTH_THROTTLE_ENABLED', True):
            return None
        # The hosting proxy must overwrite the explicitly trusted header.
        # X-Forwarded-For is deliberately not parsed or trusted.
        header = getattr(settings, 'AUTH_THROTTLE_CLIENT_IP_HEADER', 'REMOTE_ADDR')
        address = request.META.get(header) or request.META.get('REMOTE_ADDR', '')
        try:
            address = ip_address(address).compressed
        except ValueError:
            address = request.META.get('REMOTE_ADDR', 'unknown')[:64]
        identity = request.POST.get('username', request.POST.get('email', '')).strip().casefold()[:254]
        resetting = match.url_name == 'password_reset'
        kind = 'reset' if resetting else 'login'
        now = timezone.now()
        # Bounded cleanup, also for installations without a scheduled purge.
        stale = list(AuthenticationAttempt.objects.filter(expires_at__lt=now).values_list('pk', flat=True)[:100])
        AuthenticationAttempt.objects.filter(pk__in=stale).delete()
        limits = [(kind + ':ip:' + address, 10 if resetting else 100, 3600 if resetting else 900),
                  (kind + ':identity:' + address + ':' + identity, 5 if resetting else 10, 3600 if resetting else 900)]
        if identity:
            # Soft per-account ceiling independent of the client address: it stops
            # distributed guessing, yet is high enough not to lock out the owner
            # during ordinary mistakes. A successful login clears it.
            limits.append((kind + ':account:' + identity, ACCOUNT_RESET_LIMIT if resetting else ACCOUNT_LOGIN_LIMIT, 3600))
        keys = []
        for value, limit, seconds in limits:
            key = salted_hmac('cms-auth-rate', value, algorithm='sha256').hexdigest()
            wait = consume(key, limit, seconds, now)
            if wait and ':account:' in value and not resetting and self._password_matches(request, identity):
                # Limit konta chroni przed zgadywaniem z wielu adresów, ale nie może
                # blokować właściciela, który podaje poprawne hasło.
                wait = 0
            if wait:
                response = HttpResponse('Zbyt wiele prób. Spróbuj ponownie za kilka minut.', status=429, content_type='text/plain; charset=utf-8')
                response['Retry-After'] = str(wait)
                response['Cache-Control'] = 'no-store'
                return response
            if not resetting and (':identity:' in value or ':account:' in value):
                keys.append(key)
        request.auth_throttle_keys = keys
        return None

    @staticmethod
    def _password_matches(request, identity):
        from django.contrib.auth import authenticate
        password = request.POST.get('password', '')
        if not identity or not password:
            return False
        return authenticate(request, username=identity, password=password) is not None

    def process_response(self, request, response):
        keys = getattr(request, 'auth_throttle_keys', None)
        if keys and response.status_code in (301, 302, 303) and request.user.is_authenticated:
            AuthenticationAttempt.objects.filter(key__in=keys).delete()
        return response
