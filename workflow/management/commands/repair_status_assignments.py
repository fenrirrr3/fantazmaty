"""Inspect or retry the guarded repair without changing migration history."""
from importlib import import_module

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = 'Scala puste duplikaty przypisań po ręcznej zmianie statusu. Domyślnie podgląd.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Zapisz scalenie w transakcji.')
        parser.add_argument('--database', default='default')

    def handle(self, *args, **options):
        repair = import_module('workflow.migrations.0011_merge_status_assignments').repair_status_assignments
        with transaction.atomic(using=options['database']):
            report = repair(apps, options['database'], apply=options['apply'])
        self.stdout.write(f"Puste duplikaty: {report['merged']}; przypisania do przywrócenia: {report['restored']}.")
        for text_id, role, assignment_id in report['skipped']:
            self.stdout.write(f'Tekst {text_id}, rola {role}, przypisanie {assignment_id}: wymaga ręcznej kontroli; bez zmian.')
        self.stdout.write('Zapis zakończony.' if options['apply'] else 'Podgląd: niczego nie zmieniono. Zapis: --apply.')
