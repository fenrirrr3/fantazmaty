import platform
import sys
import django
from django.core.management.base import BaseCommand, CommandError

# Musi być równa wersji przypiętej w requirements.txt (pilnuje tego test komendy).
REQUIRED_DJANGO = '5.2.18'


class Command(BaseCommand):
    help = 'Sprawdza przypiętą wersję Django i pokazuje używany interpreter.'
    def handle(self, *args, **options):
        self.stdout.write(f'Python: {platform.python_version()}\nInterpreter: {sys.executable}\nDjango: {django.get_version()}')
        if django.get_version() != REQUIRED_DJANGO:
            raise CommandError(f'Wymagane Django {REQUIRED_DJANGO}. Uruchom pip install -r requirements.txt w środowisku aplikacji.')
        self.stdout.write(self.style.SUCCESS('Wersja Django zgodna z projektem i testami.'))
