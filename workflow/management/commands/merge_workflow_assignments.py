from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from workflow.assignment_merge import merge_duplicates


class Command(BaseCommand):
    help = 'Scala zwykłe przydziały tej samej roli bez usuwania etapów. Domyślnie podgląd.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--text-id', type=int)
        parser.add_argument('--database', default='default')

    def handle(self, *args, **options):
        with transaction.atomic(using=options['database']):
            report = merge_duplicates(apps, options['database'], apply=options['apply'],
                                      text_id=options['text_id'])
        for text_id, title, cycle, role, assignment_id, user_id in report['groups']:
            self.stdout.write(f'Tekst {text_id} „{title}”, przebieg {cycle}: {role}, przydział {assignment_id}, osoba {user_id}.')
        for text_id, role, reason in report['skipped']:
            self.stdout.write(f'Bez zmian: tekst {text_id}, rola {role}: {reason}')
        self.stdout.write(f"Powtórzone przydziały do scalenia: {report['merged']}.")
        self.stdout.write('Zapis zakończony.' if options['apply'] else 'Podgląd bez zapisu. Zapis: --apply.')
