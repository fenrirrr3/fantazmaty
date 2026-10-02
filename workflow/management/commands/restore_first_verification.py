"""Repair a specifically identified W1 reservation; never sweep unknown history."""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from texts.models import Text
from workflow.models import WorkflowStage as S
from workflow.reservation_repair import restore_first_verification_reservation


class Command(BaseCommand):
    help = 'Przywraca wskazaną W1 błędnie oznaczoną jako zakończona bez dat. Domyślnie podgląd.'

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument('--text-title', help='Dokładny tytuł jednego tekstu.')
        target.add_argument('--text-id', type=int)
        target.add_argument('--stage-id', type=int)
        parser.add_argument('--database', default='default')
        parser.add_argument('--apply', action='store_true',
                            help='Potwierdzam, że W1 nie została wykonana, i zapisuję korektę.')

    def handle(self, *args, **options):
        using = options['database']
        stage_id = options['stage_id']
        if stage_id is None:
            texts = Text.objects.using(using)
            if options['text_id'] is not None:
                texts = texts.filter(pk=options['text_id'])
            else:
                texts = texts.filter(title__iexact=options['text_title'].strip())
            candidates = list(texts[:2])
            if len(candidates) != 1:
                raise CommandError('Nie znaleziono dokładnie jednego tekstu. Użyj --text-id; niczego nie zmieniono.')
            text = candidates[0]
            stages = list(S.objects.using(using).filter(text=text, workflow_cycle=text.current_workflow_cycle,
                          stage_type=S.StageType.FIRST_VERIFICATION).values_list('pk', flat=True)[:2])
            if len(stages) != 1:
                raise CommandError('Tekst nie ma dokładnie jednej W1 w aktualnym przebiegu. Sprawdź historię w adminie.')
            stage_id = stages[0]
        try:
            report = restore_first_verification_reservation(stage_id, apply=options['apply'], using=using)
        except (Text.DoesNotExist, S.DoesNotExist):
            raise CommandError('Nie znaleziono wskazanego tekstu lub etapu. Niczego nie zmieniono.') from None
        except ValidationError as exc:
            raise CommandError('; '.join(exc.messages)) from exc
        self.stdout.write(f"Tekst #{report['text_id']} „{report['title']}”, W1 #{report['stage_id']}.")
        if not report['changed']:
            self.stdout.write('W1 już oczekuje na przekazanie lub rozpoczęcie. Bez zmian.')
            return
        self.stdout.write(f"Zachowano przydział #{report['assignment_id']} i osobę #{report['user_id']}; obie daty pozostają puste.")
        for field, old in report['before'].items():
            self.stdout.write(f"{field}: {old} → {report['after'][field]}")
        self.stdout.write(self.style.SUCCESS('Zapisano: W1 oczekuje na przekazanie przez redaktora.'
            if options['apply'] else 'Podgląd bez zapisu. Jeśli ta W1 nie została wykonana, zapisz z --apply.'))
