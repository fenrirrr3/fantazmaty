"""Compatibility guard for the retired notification worker."""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Wycofana komenda. Powiadomienia są wysyłane bezpośrednio po zapisie.'

    def add_arguments(self, parser):
        # Accept old task arguments so its error explains the required action.
        parser.add_argument('--watch', action='store_true')
        parser.add_argument('--interval', type=int)
        parser.add_argument('--limit', type=int)
        parser.add_argument('--stale-minutes', type=int)
        parser.add_argument('--retry-failed', action='store_true')
        parser.add_argument('--retry-disabled', action='store_true')
        parser.add_argument('--database')

    def handle(self, *args, **options):
        raise CommandError(
            'Kolejka Discorda została wyłączona. Usuń zadanie uruchamiające tę '
            'komendę. Nowe powiadomienia wysyłają się po zatwierdzeniu zapisu. '
            'Zaległe wpisy nie są automatycznie wysyłane.'
        )
