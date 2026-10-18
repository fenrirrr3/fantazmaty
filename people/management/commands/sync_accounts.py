"""Jednorazowe wyrównanie starych danych: konto logowania = e-mail osoby, osoba spoza ekipy nie loguje się."""
from django.core.management.base import BaseCommand
from django.db import connections, transaction

from people.accounts import accounts_report, apply_account_changes


def describe(changes):
    labels = []
    if 'email' in changes:
        labels.append(f'e-mail i nazwa użytkownika → {changes["email"]}')
    if changes.get('is_active') is False:
        labels.append('blokada logowania')
    return ', '.join(labels)


class Command(BaseCommand):
    help = ('Wyrównuje konta z osobami: e-mail i nazwa użytkownika konta = e-mail osoby, konto osoby spoza ekipy '
            'lub zewnętrznej zablokowane. Bez --apply tylko raport. Kolizji i osób bez e-maila nie zmienia. '
            'Kont bez osoby (np. superuserów) nie zmienia.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Wprowadza zmiany (domyślnie tylko raport).')
        parser.add_argument('--database', default='default', choices=tuple(connections))

    def handle(self, *args, **options):
        database, apply = options['database'], options['apply']
        with transaction.atomic(using=database):
            fixes, problems = accounts_report(database)
            for person, changes in fixes:
                self.stdout.write(f'{"Zmieniono" if apply else "Do zmiany"}: {person} (osoba #{person.pk}, '
                                  f'konto #{person.user_id}) – {describe(changes)}')
                if apply:
                    apply_account_changes(person.user_id, changes, database)
        for item, reason in problems:
            kind = 'osoba' if item._meta.model_name == 'person' else 'konto'
            self.stdout.write(self.style.WARNING(f'Pominięto: {item} ({kind} #{item.pk}) – {reason}'))
        summary = f'Kont do zmiany: {len(fixes)}, pominiętych: {len(problems)}.'
        if apply:
            self.stdout.write(self.style.SUCCESS(f'Zapisano. {summary}'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Podgląd – nic nie zapisano. {summary} Zapis: --apply.'))
