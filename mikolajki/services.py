"""Mikołajki: kody uczestników, wiadomości e-mail, losowanie par i limity prób z jednego adresu."""
import secrets
from ipaddress import ip_address

from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from core.auth_throttle import consume
from core.models import OptionalModules

from .models import Edition, Entry

# Bez znaków łatwych do pomylenia (0/O, 1/I/L): 31 znaków, 16 znaków kodu – około 79 bitów losowości.
CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
CODE_LENGTH = 16
CONFIRM_SALT = 'mikolajki-confirm'
# Limity z jednego adresu IP: zgłoszenia (wysyłają e-mail) i wpisanie kodu (zgadywanie kodów).
LIMITS = {'signup': (10, 3600), 'code': (20, 900)}


class DrawError(Exception):
    """Losowanie niemożliwe (komunikat dla administratora)."""


def enabled():
    return OptionalModules.enabled('secret_santa')


def new_code():
    """Kod uczestnika XXXX-XXXX-XXXX-XXXX, niepowtarzalny w bazie."""
    while True:
        raw = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        code = '-'.join(raw[index:index + 4] for index in range(0, CODE_LENGTH, 4))
        if not Entry.objects.filter(code=code).exists():
            return code


def normalize_code(value):
    """Kod wpisany przez uczestnika: wielkie litery, bez spacji i kresek, w grupach po cztery znaki."""
    raw = ''.join(char for char in (value or '').upper() if char.isalnum())[:CODE_LENGTH]
    return '-'.join(raw[index:index + 4] for index in range(0, len(raw), 4))


def find_entry(email, code):
    """Niewycofane zgłoszenie najnowszej edycji o tym adresie i kodzie albo None."""
    edition = Edition.current()
    if edition is None:
        return None
    return Entry.objects.filter(edition=edition, email=(email or '').strip().casefold(),
                                code=normalize_code(code), withdrawn_at__isnull=True).first()


def client_address(request):
    """Adres klienta jak w limicie logowania (zaufany nagłówek z ustawień, bez X-Forwarded-For)."""
    address = request.META.get(settings.AUTH_THROTTLE_CLIENT_IP_HEADER) or request.META.get('REMOTE_ADDR', '')
    try:
        return ip_address(address).compressed
    except ValueError:
        return request.META.get('REMOTE_ADDR', 'unknown')[:64]


def throttled(request, kind):
    """Liczba sekund do odczekania (0 – można) dla kolejnej próby `kind` z tego adresu."""
    if not settings.AUTH_THROTTLE_ENABLED:
        return 0
    limit, seconds = LIMITS[kind]
    key = salted_hmac('mikolajki-rate', f'{kind}:{client_address(request)}', algorithm='sha256').hexdigest()
    return consume(key, limit, seconds, timezone.now())


def confirm_token(entry):
    return signing.dumps({'entry': entry.pk, 'code': entry.code}, salt=CONFIRM_SALT, compress=True)


def entry_from_token(token):
    """Zgłoszenie z linku potwierdzającego albo None (zły podpis, inne zgłoszenie)."""
    try:
        data = signing.loads(token, salt=CONFIRM_SALT)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    return Entry.objects.filter(pk=data.get('entry'), code=data.get('code')).select_related('edition').first()


def _mail(kind, entry, request, **extra):
    """Wiadomość z szablonów mikolajki/email/<kind>_subject.txt i <kind>.txt do uczestnika `entry`."""
    context = {'entry': entry, 'edition': entry.edition, 'page_url': request.build_absolute_uri(
        reverse('mikolajki:secret_santa')), 'entry_url': request.build_absolute_uri(
        reverse('mikolajki:secret_santa_entry')), **extra}
    subject = render_to_string(f'mikolajki/email/{kind}_subject.txt', context).strip()
    body = render_to_string(f'mikolajki/email/{kind}.txt', context)
    send_mail(subject, body, None, [entry.email])


def send_signup(entry, request):
    """Podsumowanie zgłoszenia z kodem i linkiem potwierdzającym."""
    _mail('signup', entry, request, confirm_url=request.build_absolute_uri(
        reverse('mikolajki:secret_santa_confirm', args=[confirm_token(entry)])))


def send_reminder(entry, request):
    """Ponowny zapis na adres z potwierdzonym zgłoszeniem: przypomnienie kodu (bez zmiany danych)."""
    _mail('reminder', entry, request)


def send_summary(entry, request, *, changed=False):
    """Podsumowanie zgłoszenia z kodem: po dodaniu w panelu albo po zmianie danych na stronie."""
    _mail('summary', entry, request, changed=changed)


def send_letter(entry, request):
    """List wylosowanej pary (tylko list, bez danych do wysyłki)."""
    _mail('letter', entry, request, letter=entry.recipient.letter)


def send_address(entry, request):
    """Dane do wysyłki paczki dla wylosowanej pary."""
    _mail('address', entry, request, recipient=entry.recipient)


def draw(edition):
    """Losowanie par jednym zamkniętym kołem A → B → C → … → A (bez par wzajemnych), tylko raz.

    Kolejność uczestników tasuje kryptograficzne źródło losowości (secrets.SystemRandom). Biorą udział
    potwierdzone, niewycofane zgłoszenia; potrzebne są co najmniej trzy. Zwraca listę zgłoszeń w kole."""
    with transaction.atomic():
        edition = Edition.objects.select_for_update().get(pk=edition.pk)
        if edition.drawn_at:
            raise DrawError('Pary w tej edycji zostały już rozlosowane.')
        if edition.signups_open():
            raise DrawError('Najpierw zamknij zapisy.')
        entries = list(Entry.objects.select_for_update().filter(
            edition=edition, confirmed_at__isnull=False, withdrawn_at__isnull=True).order_by('pk'))
        if len(entries) < 3:
            raise DrawError('Do losowania potrzebne są co najmniej trzy potwierdzone zgłoszenia.')
        secrets.SystemRandom().shuffle(entries)
        for index, entry in enumerate(entries):
            entry.recipient = entries[(index + 1) % len(entries)]
        # Dwa kroki: OneToOne nie pozwala chwilowo na dwa takie same recipient_id.
        Entry.objects.filter(pk__in=[entry.pk for entry in entries]).update(recipient=None)
        for entry in entries:
            Entry.objects.filter(pk=entry.pk).update(recipient=entry.recipient)
        edition.drawn_at = timezone.now()
        edition.save(update_fields=['drawn_at'])
    return entries


def send_letters(edition, request):
    """Listy par do wszystkich rozlosowanych, którym jeszcze ich nie wysłano; zwraca (wysłane, błędy)."""
    sent, failed = 0, []
    for entry in Entry.objects.filter(edition=edition, recipient__isnull=False, letter_sent_at__isnull=True) \
            .select_related('recipient', 'edition'):
        try:
            send_letter(entry, request)
        except Exception:  # noqa: BLE001 – błąd serwera pocztowego: zgłoszenie zostaje do ponownej wysyłki
            failed.append(entry)
            continue
        Entry.objects.filter(pk=entry.pk).update(letter_sent_at=timezone.now())
        sent += 1
    return sent, failed
