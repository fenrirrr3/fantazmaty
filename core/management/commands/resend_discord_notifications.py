from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from core.discord_notifications import deliver_after_commit
from core.models import DiscordNotification

# Wiadomość w stanie „oczekuje” młodsza niż tyle minut może być właśnie wysyłana przez aplikację.
PENDING_GRACE = timedelta(minutes=10)
# Starszych wiadomości nie ponawiamy – zamiast licznika prób ogranicza to liczbę powtórzeń.
MAX_AGE = timedelta(days=3)


def retryable(now=None, using='default'):
    """Powiadomienia do ponowienia: oczekujące od ponad 10 minut i nieudane, nie starsze niż 3 dni.

    Stan „brak potwierdzenia” (unknown) pomijamy – wiadomość mogła dotrzeć i powtórka by ją zdublowała.
    """
    now = now or timezone.now()
    return (DiscordNotification.objects.using(using)
            .filter(Q(status='failed') | Q(status='pending', created_at__lte=now - PENDING_GRACE),
                    created_at__gte=now - MAX_AGE)
            .order_by('pk'))


def resend(now=None, using='default'):
    """Ponawia wysyłkę istniejącą funkcją dostarczania; zwraca słownik {stan końcowy: liczba}."""
    outcome = {}
    for item in retryable(now, using):
        rows = DiscordNotification.objects.using(using)
        # Nieudana wysyłka wraca do kolejki; deliver() przyjmuje wyłącznie stan „oczekuje”.
        rows.filter(pk=item.pk, status='failed').update(status='pending', sending_started_at=None)
        deliver_after_commit(item.pk, using)
        status = rows.filter(pk=item.pk).values_list('status', flat=True).first()
        outcome[status] = outcome.get(status, 0) + 1
    return outcome


class Command(BaseCommand):
    help = ('Ponawia wysyłkę powiadomień Discord oczekujących od ponad 10 minut albo nieudanych (nie starszych niż '
            '3 dni; stan „brak potwierdzenia” nie jest ponawiany). Uruchamiać co kilkanaście minut.')

    def handle(self, *args, **options):
        outcome = resend()
        total = sum(outcome.values())
        labels = dict(DiscordNotification._meta.get_field('status').choices)
        summary = ', '.join(f'{labels.get(status, status)}: {count}' for status, count in sorted(outcome.items()))
        self.stdout.write(f'Ponowione powiadomienia: {total}' + (f' ({summary})' if summary else ''))
