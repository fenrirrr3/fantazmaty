"""Import completed translations into existing empty anthologies; preview by default."""
import hashlib
import json
from collections import Counter
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction

from texts.models import Anthology, Text, ForeignAuthor, Translator, TextTranslation
from workflow.catalog import all_stage_roles
from workflow.completed_import import import_completed_workflow
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.management.commands.import_completed_table import Command as TableCommand, normalized


COLUMN_TYPES = {
    'Redaktor': 'editing', 'Koordynator redakcji': 'editing_control',
    'Korektor 1': 'first_proofreading', 'Korektor 2': 'second_proofreading',
    'Korektor 3': 'third_proofreading', 'Korektor 4': 'fourth_proofreading',
    'Weryfikator 1': 'first_verification', 'Weryfikator 2': 'second_verification',
    'Weryfikator 3': 'third_verification', 'Weryfikator 3 (wyk. 2)': 'third_verification',
    'Koordynator weryfikacji': 'coordinator_control',
}


class Command(TableCommand):
    help = 'Import zakończonych tłumaczeń bez dat. Domyślnie podgląd, zapis z --apply.'

    def add_arguments(self, parser):
        parser.add_argument('file')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', default='raport_tlumaczenia')

    def validate(self, data):
        if not isinstance(data, dict) or set(data) != {'schema_version', 'source', 'texts'}:
            raise ValueError('Wymagane schema_version, source, texts.')
        if data['source'] != 'completed-translations-2026-10-05':
            raise ValueError('Nie zmieniaj identyfikatora źródła importu.')
        if not isinstance(data['texts'], list) or not data['texts']:
            raise ValueError('Brak tekstów.')
        legacy_rows = []
        fields = {'source_row', 'title', 'anthology', 'length', 'length_source',
                  'foreign_authors', 'translators', 'original_verifier', 'stages', 'omitted_columns'}
        for row in data['texts']:
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError('Nieprawidłowe pola tekstu.')
            if not isinstance(row['original_verifier'], str) or len(row['original_verifier']) > 255:
                raise ValueError('Nieprawidłowe pole Weryfikacja z oryginałem.')
            for field, id_field in [('foreign_authors', 'foreign_author_id'), ('translators', 'translator_id')]:
                if not isinstance(row[field], list) or not row[field]:
                    raise ValueError(f'Brak listy {field}.')
                for name in row[field]:
                    self.validate_name(name, id_field)
            if not isinstance(row['stages'], list) or not isinstance(row['omitted_columns'], list):
                raise ValueError('Wymagane listy etapów i pustych kolumn.')
            occupied = Counter()
            for stage in row['stages']:
                if (not isinstance(stage, dict) or stage.get('source_column') not in COLUMN_TYPES
                        or stage.get('stage_type') != COLUMN_TYPES[stage['source_column']]):
                    raise ValueError('Rola musi odpowiadać kolumnie źródłowej; bez przesuwania osób.')
                occupied[stage['source_column']] += 1
            if occupied['Weryfikator 3 (wyk. 2)'] and (occupied['Weryfikator 3'] != 1 or occupied['Weryfikator 3 (wyk. 2)'] != 1):
                raise ValueError('Weryfikator 3 (wyk. 2) wymaga dokładnie jednego wykonania 1.')
            column_order = list(COLUMN_TYPES)
            positions = [column_order.index(s['source_column']) for s in row['stages']]
            if positions != sorted(positions):
                raise ValueError('Nieprawidłowa kolejność kolumn źródłowych.')
            omitted = []
            for missing in row['omitted_columns']:
                if (not isinstance(missing, dict) or set(missing) != {'source_column', 'reason'}
                        or missing['source_column'] not in COLUMN_TYPES
                        or missing['reason'] not in ('puste pole', 'nie było korekty')):
                    raise ValueError('Nieprawidłowe dane pustej kolumny.')
                omitted.append(missing['source_column'])
            if (len(set(omitted)) != len(omitted) or set(omitted) & set(occupied)
                    or set(omitted) | set(occupied) != set(COLUMN_TYPES)):
                raise ValueError('Każda kolumna musi mieć wykonawcę albo oznaczenie braku wykonania.')
            legacy = {k: row[k] for k in ('source_row', 'title', 'anthology', 'length', 'length_source', 'stages')}
            legacy['authors'] = [{k: p[k] for k in ('first_name', 'last_name')} for p in row['foreign_authors']]
            legacy_rows.append(legacy)
        super().validate(dict(schema_version=data['schema_version'], source=data['source'], texts=legacy_rows))

    def person(self, name):
        key = json.dumps(name, sort_keys=True, ensure_ascii=False)
        if key not in self.people_cache:
            self.people_cache[key] = super().person(name)
        return self.people_cache[key]

    def profile(self, model, name, id_field):
        key = model._meta.label, json.dumps(name, sort_keys=True, ensure_ascii=False)
        if key not in self.profile_cache:
            obj = self.resolve(model, name, id_field)
            if obj is None:
                obj = model(first_name=name['first_name'].strip(), last_name=name['last_name'].strip(), email='')
                obj.full_clean()
                obj.save()
                self.events.append({'action': 'nowy ' + str(model._meta.verbose_name), 'name': str(obj), 'id': obj.pk})
            self.profile_cache[key] = obj
        return self.profile_cache[key]

    def import_row(self, row, source, anthology):
        before = len(self.events)
        foreign = [self.profile(ForeignAuthor, p, 'foreign_author_id') for p in row['foreign_authors']]
        translators = [self.profile(Translator, p, 'translator_id') for p in row['translators']]
        if len({p.pk for p in foreign}) != len(foreign) or len({p.pk for p in translators}) != len(translators):
            raise ValueError('Powtórzona osoba na liście autorów zagranicznych lub tłumaczy.')
        stages = [{'stage_type': s['stage_type'], 'assigned_to_id': self.person(s['person']).pk} for s in row['stages']]
        texts = list(Text.objects.select_for_update().filter(import_source=source, import_source_row=row['source_row']))
        if len(texts) > 1:
            raise ValueError('Powtórzony identyfikator importu w bazie.')
        existing = bool(texts)
        if existing:
            text = texts[0]
            translation = TextTranslation.objects.select_for_update().filter(text=text).first()
            if (text.title != row['title'].strip() or text.anthology_id != anthology.pk or text.length != row['length']
                    or text.authors.exists() or translation is None
                    or translation.original_verifier != row['original_verifier'].strip()
                    or set(translation.foreign_authors.values_list('pk', flat=True)) != {p.pk for p in foreign}
                    or set(translation.translators.values_list('pk', flat=True)) != {p.pk for p in translators}):
                raise ValueError('Istniejący tekst różni się od importu. Nie nadpisano go.')
            # Also reject altered execution numbers, completion flags and work dates.
            recorded = list(WorkflowStage.objects.select_for_update().filter(text=text).exclude(stage_type='ready').order_by('pk'))
            numbers = Counter()
            if len(recorded) != len(stages):
                raise ValueError('Zmieniona liczba etapów istniejącego tekstu.')
            for stage, expected in zip(recorded, stages):
                numbers[expected['stage_type']] += 1
                number = numbers[expected['stage_type']]
                assignment = stage.assignment
                if (stage.stage_type != expected['stage_type'] or not stage.is_completed or not stage.imported_completed
                        or stage.execution_number != number or stage.iteration != number or stage.queued_at is not None
                        or assignment is None or assignment.execution_number != number
                        or assignment.role != all_stage_roles()[expected['stage_type']]
                        or assignment.assigned_to_id != expected['assigned_to_id']):
                    raise ValueError('Workflow zmieniono po imporcie. Nie nadpisano go.')
            if WorkflowRoleAssignment.objects.filter(text=text).count() != len(stages):
                raise ValueError('Zmieniona liczba przypisań istniejącego tekstu.')
        else:
            text = Text(title=row['title'].strip(), length=row['length'], import_source=source, import_source_row=row['source_row'])
            text.full_clean()
            text.save()
        import_completed_workflow(text_id=text.pk, stages=stages, next_stage='ready', preserve_executions=True)
        if not existing:
            text.anthology = anthology
            text.full_clean()
            text.save(update_fields=['anthology'])
            translation, _ = TextTranslation.objects.get_or_create(text=text)
            translation.original_verifier = row['original_verifier'].strip()
            translation.full_clean()
            translation.save(update_fields=['original_verifier'])
            translation.foreign_authors.set(foreign)
            translation.translators.set(translators)
        executions = Counter()
        works = []
        for stage in row['stages']:
            kind = stage['stage_type']; executions[kind] += 1
            works.append({'column': stage['source_column'], 'stage_type': kind, 'execution_number': executions[kind],
                          'person': stage['person']['first_name'] + ' ' + stage['person']['last_name']})
        return {'row': row['source_row'], 'title': text.title, 'anthology': anthology.title, 'text_id': text.pk,
                'length': text.length, 'result': 'bez zmian' if existing else 'dodanie',
                'foreign_authors': [{'id': p.pk, 'name': str(p)} for p in foreign],
                'translators': [{'id': p.pk, 'name': str(p)} for p in translators],
                'Weryfikacja z oryginałem': translation.original_verifier,
                'completed_executions': works, 'omitted_columns': row['omitted_columns'], 'events': self.events[before:]}

    def handle(self, *args, **options):
        self.events, self.warnings = [], []
        self.people_cache, self.profile_cache = {}, {}
        report = {'mode': 'przerwano — nic nie zapisano', 'input_sha256': None, 'counts': {},
                  'anthologies': [], 'texts': [], 'conflicts': [], 'warnings': self.warnings,
                  'dates': 'Nieznane daty pracy i przypisania pozostają puste.',
                  'identities': 'Osobne profile autorów zagranicznych i tłumaczy. Brakujące profile wykonawców: pusty e-mail, konto nieaktywne bez hasła i bez nadawania ról.',
                  'notice': 'W podglądzie identyfikatory nowo tworzonych rekordów są tymczasowe; żadne zmiany nie są utrwalane.'}
        target = Path(options['report']).with_suffix('.json')
        source_path = Path(options['file'])
        if target.resolve() == source_path.resolve():
            raise CommandError('Plik raportu nie może być plikiem danych importu.')

        def write_report():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

        current_row = None
        try:
            payload = source_path.read_bytes()
            report['input_sha256'] = hashlib.sha256(payload).hexdigest()
            data = json.loads(payload.decode('utf-8-sig'))
            self.validate(data)
            with transaction.atomic():
                anthologies = {}
                for title in sorted({r['anthology'] for r in data['texts']}):
                    matches = [a for a in Anthology.objects.select_for_update().order_by('pk') if normalized(a.title) == normalized(title)]
                    if len(matches) != 1:
                        raise ValueError(f'Antologia „{title}” musi wskazywać dokładnie jeden istniejący rekord.')
                    anthology = matches[0]
                    expected = [r['source_row'] for r in data['texts'] if r['anthology'] == title]
                    if Text.objects.filter(anthology=anthology).exclude(import_source=data['source'], import_source_row__in=expected).exists():
                        raise ValueError(f'Antologia „{title}” zawiera teksty spoza tego importu; wymagana pusta antologia.')
                    report['anthologies'].append({'id': anthology.pk, 'title': anthology.title,
                                                 'is_translated_before': anthology.is_translated, 'is_translated_after': True})
                    if not anthology.is_translated:
                        # Do not invoke bulk synchronization on pre-existing texts during a retry.
                        if Text.objects.filter(anthology=anthology).exists():
                            raise ValueError(f'Antologia „{title}” przestała być tłumaczona po imporcie; wymaga sprawdzenia.')
                        anthology.is_translated = True
                        anthology.save(update_fields=['is_translated'])
                    anthologies[title] = anthology
                for row in data['texts']:
                    current_row = row['source_row']
                    report['texts'].append(self.import_row(row, data['source'], anthologies[row['anthology']]))
                report['counts'] = {'texts_added': sum(r['result'] == 'dodanie' for r in report['texts']),
                                    'texts_unchanged': sum(r['result'] == 'bez zmian' for r in report['texts']),
                                    'completed_executions': sum(len(r['completed_executions']) for r in report['texts']),
                                    'profiles': dict(Counter(e['action'] for e in self.events))}
                report['mode'] = 'zapisano' if options['apply'] else 'podgląd — nic nie zapisano'
                # An unwritable report aborts the database transaction as well.
                write_report()
                if not options['apply']:
                    transaction.set_rollback(True)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, ValidationError, DatabaseError) as exc:
            report['mode'] = 'wycofano — nic nie zapisano'
            report['conflicts'].append({'row': current_row, 'error': str(exc)})
            try:
                write_report()
            except OSError as report_error:
                self.stderr.write(f'Nie udało się zapisać raportu: {report_error}')
            self.stderr.write(json.dumps(report, ensure_ascii=False, indent=2))
            raise CommandError('Import przerwany; nie zapisano żadnych zmian. Szczegóły w raporcie.') from exc
        self.stdout.write(f"{report['mode']}. Nowe teksty: {report['counts']['texts_added']}; bez zmian: {report['counts']['texts_unchanged']}; wykonania: {report['counts']['completed_executions']}. Raport: {target}")
