"""Convert generated miniature workflows to one text per Extract anthology."""
import json
from pathlib import Path
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from texts.models import Anthology
from texts.extract_whole import consolidate_volume


class Command(BaseCommand):
    help = 'Jedna pozycja tekstu na tom Ekstraktów; domyślnie podgląd bez zapisu.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', default='raport_ekstrakty_calosciowo.json')

    def handle(self, *args, **options):
        report = {'mode': 'podgląd – nic nie zapisano', 'volumes': [], 'conflicts': []}
        path = Path(options['report'])
        # Verify report destination before changing the database.
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        try:
            with transaction.atomic():
                books = Anthology.objects.filter(Q(title__icontains='ekstrakty') | Q(extract_volume__isnull=False)).order_by('pk')
                for book in books:
                    report['volumes'].append(consolidate_volume(book))
                if not options['apply']:
                    transaction.set_rollback(True)
                else:
                    report['mode'] = 'zapisano'
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        except (ValidationError, ValueError) as exc:
            report['mode'] = 'przerwano – nic nie zapisano'
            report['conflicts'] = getattr(exc, 'messages', [str(exc)])
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            raise CommandError(f"Konwersja przerwana. Szczegóły: {path}") from exc
        self.stdout.write(f"{report['mode']}; tomy: {len(report['volumes'])}; raport: {path}")
