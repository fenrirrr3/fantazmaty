"""Create missing standard roles without creating accounts or editorial data."""
from django.core.management.base import BaseCommand
from django.db import connections, transaction
from people.models import Role
from people.role_ordering import TEAM_ROLE_ORDER


class Command(BaseCommand):
    help = "Uzupełnia standardowe role zespołu. Można uruchamiać wielokrotnie."

    def add_arguments(self, parser):
        parser.add_argument('--database', default='default', choices=tuple(connections))

    def handle(self, *args, **options):
        database = options['database']
        names = tuple(dict.fromkeys((*TEAM_ROLE_ORDER, 'Ilustrator', 'Korektor audiobooków')))
        created = 0
        with transaction.atomic(using=database):
            for name in names:
                _, added = Role.objects.using(database).get_or_create(
                    name__iexact=name, defaults={'name': name},
                )
                created += added
        self.stdout.write(self.style.SUCCESS(
            f"Dodano ról: {created}. Standardowy zestaw: {len(names)}."
        ))
