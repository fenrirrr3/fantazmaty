"""Inspect or retry the guarded repair without changing migration history."""
from importlib import import_module

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = 'Przywraca nieaktualne przypisania zespołu i scala puste rekordy. Domyślnie podgląd.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Zapisz scalenie w transakcji.')
        parser.add_argument('--database', default='default')
        parser.add_argument('--inspect-text', help='Wypisz przydziały i etapy wskazanego tytułu, bez zapisu.')

    def handle(self, *args, **options):
        if options['inspect_text']:
            if options['apply']:
                raise CommandError('Podglądu tekstu nie łączy się z --apply.')
            Text = apps.get_model('texts', 'Text')
            A = apps.get_model('workflow', 'WorkflowRoleAssignment')
            S = apps.get_model('workflow', 'WorkflowStage')
            texts = Text.objects.using(options['database']).filter(title__iexact=options['inspect_text'])
            if not texts.exists():
                raise CommandError('Nie znaleziono tekstu o podanym tytule.')
            for text in texts:
                self.stdout.write(f'Tekst {text.pk} „{text.title}”, aktualny przebieg {text.current_workflow_cycle}.')
                for row in A.objects.using(options['database']).filter(text=text).order_by('workflow_cycle', 'role', 'execution_number'):
                    self.stdout.write(f'Przydział {row.pk}: {row.role}, przebieg={row.workflow_cycle}, wykonanie={row.execution_number}, aktualny={row.is_current}, osoba={row.assigned_to_id}.')
                for row in S.objects.using(options['database']).filter(text=text).order_by('workflow_cycle', 'pk'):
                    self.stdout.write(f'Etap {row.pk}: {row.stage_type}, przebieg={row.workflow_cycle}, przydział={row.assignment_id}, aktualny={row.is_current}, zakończony={row.is_completed}, daty={row.started_at}/{row.ended_at}.')
            return
        repair = import_module('workflow.migrations.0012_restore_retired_team_assignments').restore_team_assignments
        with transaction.atomic(using=options['database']):
            report = repair(apps, options['database'], apply=options['apply'])
        self.stdout.write(f"Puste duplikaty: {report['merged']}; przypisania do przywrócenia: {report['restored']}.")
        for text_id, title, role, assignment_id, user_id in report['assignments']:
            self.stdout.write(f'Tekst {text_id} „{title}”: {role}, przydział {assignment_id}, osoba {user_id}.')
        for text_id, role, assignment_id in report['skipped']:
            self.stdout.write(f'Tekst {text_id}, rola {role}, przypisanie {assignment_id}: wymaga ręcznej kontroli; bez zmian.')
        self.stdout.write('Zapis zakończony.' if options['apply'] else 'Podgląd: niczego nie zmieniono. Zapis: --apply.')
