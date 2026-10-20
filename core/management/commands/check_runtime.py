import platform
import sys
import django
from django.core.management.base import BaseCommand, CommandError

from core.runtime_checks import pinned_django_version


class Command(BaseCommand):
    help = 'Sprawdza przypiętą wersję Django i pokazuje używany interpreter.'
    def handle(self, *args, **options):
        self.stdout.write(f'Python: {platform.python_version()}\nInterpreter: {sys.executable}\nDjango: {django.get_version()}')
        required = pinned_django_version()
        if required is None:
            raise CommandError('Nie udało się odczytać przypiętej wersji Django z requirements.txt.')
        if django.get_version() != required:
            raise CommandError(f'Wymagane Django {required}. Uruchom pip install -r requirements.txt w środowisku aplikacji.')
        self.stdout.write(self.style.SUCCESS('Wersja Django zgodna z projektem i testami.'))
