from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Sprawdza powtarzające się adresy kont przed migracją; nie zmienia danych.'

    def handle(self, **options):
        groups = {}
        for pk, email in get_user_model().objects.values_list('pk', 'email').iterator():
            key = (email or '').strip().lower()
            if key: groups.setdefault(key, []).append(pk)
        duplicates = [ids for ids in groups.values() if len(ids) > 1]
        if duplicates:
            for ids in duplicates: self.stdout.write('Konta z tym samym adresem – ID: ' + ', '.join(map(str, ids)))
            raise CommandError('Popraw lub scal wskazane konta przed migracją. Niczego nie zmieniono.')
        self.stdout.write(self.style.SUCCESS('Brak powtarzających się niepustych adresów kont.'))
