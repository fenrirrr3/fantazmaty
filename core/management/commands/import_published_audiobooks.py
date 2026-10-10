"""Atomic, repeatable import of confirmed historical audiobook publications."""
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.audiobook_validators import validate_audio_links
from core.edit_versions import batched_bumps
from core.models import Audiobook, AudiobookStage
from texts.models import Text


def name_key(value):
    return ' '.join(value.casefold().split())


def known_date(value):
    if value is None:
        return None
    result = date.fromisoformat(value)
    if result > timezone.localdate() or result.year < 2018:
        raise ValueError(f'Nieprawidłowa data historyczna: {value}')
    return result


class Command(BaseCommand):
    help = 'Import opublikowanych audiobooków; domyślnie podgląd bez zapisu.'
    schema = 'fantazmaty-published-audio-v1'

    def add_arguments(self, parser):
        parser.add_argument('input')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--report', default='raport_audiobooki.json')
        parser.add_argument('--people-map', help='Opcjonalny JSON: imię i nazwisko korektora -> ID istniejącego konta.')

    def handle(self, *args, **options):
        report_path = Path(options['report'])
        if report_path.suffix.lower() != '.json':
            report_path = Path(str(report_path) + '.json')
        source_path = Path(options['input'])
        if report_path.resolve() == source_path.resolve():
            raise CommandError('Raport nie może nadpisać pliku danych.')
        report = {'mode': 'podgląd – nic nie zapisano', 'counts': {}, 'texts': [], 'conflicts': [], 'warnings': []}
        self.report, self.counts = report, Counter()
        try:
            raw = source_path.read_bytes()
            data = json.loads(raw.decode('utf-8-sig'))
            report['input_sha256'] = hashlib.sha256(raw).hexdigest()
            if data.get('schema') != self.schema or not isinstance(data.get('records'), list):
                raise ValueError('Nieobsługiwany format importu audiobooków.')
            self.people_map = json.loads(Path(options['people_map']).read_text(encoding='utf-8-sig')) if options['people_map'] else {}
            if not isinstance(self.people_map, dict):
                raise ValueError('Mapa osób musi być obiektem JSON: nazwisko -> ID konta.')
            self.users = {u.pk: u for u in get_user_model().objects.select_related('person_profile')}
            self.names = defaultdict(set)
            for user in self.users.values():
                if user.get_full_name().strip():
                    self.names[name_key(user.get_full_name())].add(user.pk)
                profile = getattr(user, 'person_profile', None)
                if profile:
                    self.names[name_key(str(profile))].add(user.pk)
            seen = set()
            # Same Text locks as production/admin; no updates to editorial work,
            # visibility flags, accounts, roles or contact e-mail addresses.
            with transaction.atomic():
                with batched_bumps():
                    for row in sorted(data['records'], key=lambda r: (r.get('anthology', ''), r.get('title', ''))):
                        key = (row.get('anthology'), row.get('title'))
                        try:
                            if key in seen:
                                raise ValueError('Powtórzony tekst w pliku importu.')
                            seen.add(key)
                            with transaction.atomic():
                                result = self.import_row(row)
                            report['texts'].append(result)
                        except (ValueError, TypeError, KeyError, ValidationError) as exc:
                            report['conflicts'].append({'anthology': key[0], 'title': key[1], 'error': str(exc)})
                if report['conflicts'] or not options['apply']:
                    transaction.set_rollback(True)
                else:
                    report['mode'] = 'zapisano'
                # Write report before commit, so a filesystem error rolls back.
                report['counts'] = dict(self.counts)
                if report['conflicts']:
                    report['mode'] = 'wycofano – nic nie zapisano'
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise CommandError(f'Nie zapisano importu: {exc}') from exc
        self.stdout.write(f"{report['mode']}. Teksty: {len(report['texts'])}; konflikty: {len(report['conflicts'])}. Raport: {report_path}")
        if report['conflicts']:
            raise CommandError('Import przerwany; nie zapisano żadnych zmian. Szczegóły w raporcie.')

    def resolve_person(self, name):
        if not name:
            return None
        if name in self.people_map:
            pk = self.people_map[name]
            if type(pk) is not int or pk not in self.users:
                raise ValueError(f'Nieistniejące ID konta dla {name}.')
            return self.users[pk]
        names = [name]
        # Explicitly confirmed by the owner of the supplied table.
        if name == 'Jagoda Glaza':
            names.append('Jadwiga Glaza')
        ids = set().union(*(self.names[name_key(n)] for n in names))
        if len(ids) != 1:
            raise ValueError(f'Korektor {name}: znaleziono {len(ids)} kont. Wskaż istniejące konto przez --people-map. Nie tworzę kont ani ról.')
        return self.users[ids.pop()]

    def import_row(self, row):
        if row['status'] != 'published':
            raise ValueError('Ten import obsługuje wyłącznie Opublikowane.')
        matches = list(Text.objects.select_for_update().filter(
            anthology__title=row['anthology'], title=row['title']).order_by('pk'))
        # Python comparison also guards against case-insensitive database collations.
        matches = [t for t in matches if t.title == row['title'] and t.anthology.title == row['anthology']]
        if len(matches) != 1:
            raise ValueError(f'Wymagany jeden istniejący tekst o dokładnym tytule i antologii; znaleziono {len(matches)}.')
        text = matches[0]
        if text.anthology.is_novel:
            raise ValueError('Import nie obejmuje rozdziałów powieści.')
        audio = Audiobook.objects.filter(text=text).first()
        created = audio is None
        if audio is None:
            audio = Audiobook(text=text)
        if audio.active_stage_id or audio.status not in ('pending', 'published') or text.audiobook_stages.filter(is_completed=False).exists():
            raise ValueError('Audiobook ma trwającą produkcję. Nie nadpisano statusu ani etapów.')
        links = [{k: p[k] for k in ('service', 'part', 'url')} for p in row['publications']]
        validate_audio_links(links)
        if not links:
            raise ValueError('Brak potwierdzonej publikacji.')
        pairs = [(p['service'], p['part']) for p in links]
        if len(pairs) != len(set(pairs)):
            raise ValueError('Niejednoznaczny link do tej samej części w serwisie.')
        premiere = known_date(row['premiere_date'])
        dates = [known_date(p['date']) for p in row['publications']]
        if not all(dates) or premiere != min(dates):
            raise ValueError('Premiera musi być najwcześniejszą potwierdzoną datą z serwisów.')
        proofreader = self.resolve_person(row.get('proofreader', ''))
        desired = {'status': 'published', 'premiere_date': premiere}
        for field, key in [('narrator_name', 'narrator'), ('engineer_name', 'engineer')]:
            if row.get(key):
                desired[field] = row[key]
        if proofreader:
            desired['proofreader_id'] = proofreader.pk
            profile = getattr(proofreader, 'person_profile', None)
            if not proofreader.is_active or not profile or not profile.is_active:
                self.report['warnings'].append(f'{text.title}: zachowano historyczną pracę konta nieaktywnego / bez profilu: {row["proofreader"]}.')
        extras = []
        for service in ('youtube', 'hearthis'):
            service_links = sorted((p for p in links if p['service'] == service), key=lambda p: p['part'])
            if service_links:
                desired[f'{service}_url'] = service_links[0]['url']
                extras.extend(service_links[1:])
        # Keep unrelated links previously added by a coordinator.
        imported_urls = {p['url'] for p in links}
        extras.extend(p for p in audio.additional_links if p['url'] not in imported_urls)
        desired['additional_links'] = extras
        stages = []
        for index, stage in enumerate(row.get('proofreading', [])):
            start, end = known_date(stage['started_at']), known_date(stage['ended_at'])
            if not (start or end) or (start and end and end < start):
                raise ValueError('Nieprawidłowy zakres dat korekty.')
            performer = self.resolve_person(stage['proofreader'])
            if not performer:
                raise ValueError('Historyczny etap korekty wymaga istniejącego konta wykonawcy.')
            if end and end > premiere:
                self.report['warnings'].append(f'{text.title}: korekta zakończona {end}, po pierwszej publikacji {premiere}; daty zachowano zgodnie ze źródłami.')
            import_key = hashlib.sha256(json.dumps(['published-audio-2026', row['anthology'], row['title'], index], ensure_ascii=False).encode()).hexdigest()
            values = {'text_id': text.pk, 'stage_type': 'proofreading', 'started_at': start,
                'ended_at': end, 'performer_id': performer.pk, 'is_completed': True}
            old = AudiobookStage.objects.filter(import_key=import_key).first()
            if old:
                if any(getattr(old, k) != v for k, v in values.items()):
                    raise ValueError('Wcześniej importowany etap został zmieniony. Wymaga ręcznego porównania.')
                stages.append({'id': old.pk, 'action': 'bez zmian'})
                continue
            equivalent = list(text.audiobook_stages.filter(stage_type='proofreading',
                started_at=start, ended_at=end, is_completed=True))
            if len(equivalent) > 1 or (equivalent and (equivalent[0].import_key or equivalent[0].performer_id not in (None, performer.pk))):
                raise ValueError('Istniejąca historia korekty jest niejednoznaczna; nie dodano duplikatu.')
            stage_obj = equivalent[0] if equivalent else AudiobookStage(**values)
            stage_obj.performer = performer
            stage_obj.import_key = import_key
            stage_obj.full_clean()
            stage_obj.save()
            stages.append({'id': stage_obj.pk, 'action': 'uzupełniono' if equivalent else 'dodano',
                'performer_id': performer.pk, 'started_at': start, 'ended_at': end})
        starts = [known_date(s['started_at']) for s in row.get('proofreading', []) if s['started_at']]
        if starts:
            desired['proofreading_started_at'] = min(starts)
        changes = {field: {'old': getattr(audio, field), 'new': value}
            for field, value in desired.items() if getattr(audio, field) != value}
        for field, value in desired.items():
            setattr(audio, field, value)
        # Historical assignment may refer to a former member. Validate field values
        # and constraints without requiring today's active production role.
        audio.clean_fields()
        audio.validate_unique()
        audio.validate_constraints()
        if created or changes:
            audio.save()
        self.counts['new_audiobooks' if created else 'updated_audiobooks' if changes else 'unchanged_audiobooks'] += 1
        self.counts['stages_added'] += sum(s['action'] == 'dodano' for s in stages)
        self.counts['links'] += len(links)
        if not text.for_recording or text.audiobook_blacklisted:
            self.report['warnings'].append(f'{text.title}: zapisano historię, ale nie zmieniono wyłączenia nagrywania / czarnej listy.')
        return {'text_id': text.pk, 'anthology': row['anthology'], 'title': row['title'],
            'proofreader_id': proofreader.pk if proofreader else None, 'changes': changes, 'stages': stages}
