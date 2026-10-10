"""Import the supplied production table without overwriting later CMS work."""
import hashlib
import json

from core.management.commands.import_published_audiobooks import Command as BaseImport, known_date
from core.audiobook_services import DATE_FIELDS
from core.models import Audiobook, AudiobookStage, AudioContributor
from texts.models import Text


class Command(BaseImport):
    help = 'Import nieopublikowanych audiobooków; domyślnie podgląd, zapis przez --apply.'
    schema = 'fantazmaty-pending-audio-v1'

    def import_row(self, row):
        if row['status'] not in set(Audiobook.Status.values) - {'published'}:
            raise ValueError('Plik ma zawierać tylko audiobooki nieopublikowane.')
        matches = list(Text.objects.select_for_update().filter(title=row['title'], anthology__title=row['anthology']).order_by('pk'))
        matches = [t for t in matches if t.title == row['title'] and t.anthology.title == row['anthology']]
        if len(matches) != 1:
            raise ValueError(f'Wymagany jeden dokładny tytuł w podanej antologii; znaleziono {len(matches)}.')
        text = matches[0]
        if text.anthology.is_novel or text.anthology.status == 'abandoned':
            raise ValueError('Import nie obejmuje powieści ani porzuconych antologii.')
        audio = Audiobook.objects.filter(text=text).first()
        created = audio is None
        if audio is None:
            audio = Audiobook(text=text)
        proofreader = self.resolve_person(row.get('proofreader', ''))
        desired = {'status': row['status']}
        for field, key in [('narrator_name', 'narrator'), ('engineer_name', 'engineer')]:
            if row.get(key):
                desired[field] = row[key]
                if not getattr(audio, f'{key}_email'):
                    contacts = list(AudioContributor.objects.filter(name__iexact=row[key])[:2])
                    if len(contacts) > 1:
                        raise ValueError(f'Niejednoznaczny profil: {row[key]}. Uporządkuj kontakty przed importem.')
                    if contacts:
                        desired[f'{key}_email'] = contacts[0].email
        if proofreader:
            desired['proofreader_id'] = proofreader.pk
        specs = []
        active_key = None
        for index, source in enumerate(row['stages']):
            kind = source['type']
            if kind not in set(Audiobook.Status.values) - {'published'}:
                raise ValueError('Nieprawidłowy etap w danych.')
            start, end = known_date(source.get('started_at')), known_date(source.get('ended_at'))
            complete, active = source.get('completed', False), source.get('active', False)
            if type(complete) is not bool or type(active) is not bool:
                raise ValueError('Pola completed i active muszą być booleanami.')
            if (end and not complete) or (start and end and end < start) or (complete and active):
                raise ValueError('Niespójny stan lub daty etapu.')
            if active and (active_key or kind != row['status']):
                raise ValueError('Tylko jeden bieżący etap zgodny ze statusem audiobooka.')
            key = hashlib.sha256(json.dumps(['pending-audio-2026-10', row['anthology'], row['title'], index], ensure_ascii=False).encode()).hexdigest()
            if active:
                active_key = key
            if start and kind in DATE_FIELDS:
                desired[DATE_FIELDS[kind]] = start
            values = {'text_id': text.pk, 'stage_type': kind, 'started_at': start, 'ended_at': end,
                'is_completed': complete, 'performer_id': proofreader.pk if proofreader and kind == 'proofreading' else None}
            specs.append((key, values))
        if not specs:
            raise ValueError('Brak etapów w pliku.')
        existing = list(text.audiobook_stages.order_by('pk'))
        expected = dict(specs)
        imported = bool(existing) and all(s.import_key in expected for s in existing)
        if imported:
            if len(existing) != len(specs) or any(any(getattr(s, f) != v for f, v in expected[s.import_key].items()) for s in existing):
                raise ValueError('Zaimportowana historia została zmieniona. Nie nadpisano późniejszej pracy.')
            active_id = next((s.pk for s in existing if s.import_key == active_key), None)
            if any(getattr(audio, f) != value for f, value in desired.items()) or audio.active_stage_id != active_id:
                raise ValueError('Audiobook zmienił się po imporcie. Nie cofnięto produkcji.')
            self.counts['unchanged_audiobooks'] += 1
            return {'text_id': text.pk, 'title': text.title, 'action': 'bez zmian'}
        if existing or audio.active_stage_id or audio.status != 'pending' or audio.youtube_url or audio.hearthis_url or audio.premiere_date or audio.additional_links:
            raise ValueError('Audiobook ma już produkcję lub publikację. Import nie nadpisuje istniejącej pracy.')
        for field in DATE_FIELDS.values():
            if getattr(audio, field) and getattr(audio, field) != desired.get(field):
                raise ValueError(f'Istniejąca data {field} jest inna niż w tabeli. Wymaga porównania.')
        for field, value in desired.items():
            old = getattr(audio, field)
            if field != 'status' and old not in ('', None, value):
                raise ValueError(f'Konflikt w polu {field}: istnieje już inna wartość.')
        saved = []
        for key, values in specs:
            stage = AudiobookStage(import_key=key, **values)
            stage.full_clean()
            stage.save()
            saved.append(stage)
            if key == active_key:
                desired['active_stage_id'] = stage.pk
        for field, value in desired.items():
            setattr(audio, field, value)
        # Historic credits require an existing account, without granting roles.
        audio.clean_fields()
        audio.validate_unique()
        audio.validate_constraints()
        audio.save()
        self.counts['new_audiobooks' if created else 'updated_audiobooks'] += 1
        self.counts['stages_added'] += len(saved)
        if not text.for_recording or text.audiobook_blacklisted:
            self.report['warnings'].append(f'{text.title}: zachowano wyłączenie nagrywania / czarną listę.')
        if proofreader:
            from core.audiobook_forms import eligible_proofreaders
            if not eligible_proofreaders().filter(pk=proofreader.pk).exists():
                self.report['warnings'].append(f'{text.title}: konto {row["proofreader"]} nie ma obecnie aktywnej roli Korektor audiobooków; import nie nadaje uprawnień.')
        return {'text_id': text.pk, 'anthology': row['anthology'], 'title': text.title, 'status': audio.status,
            'proofreader_id': audio.proofreader_id, 'stages': [{'id': s.pk, 'type': s.stage_type,
                'started_at': s.started_at, 'ended_at': s.ended_at, 'completed': s.is_completed} for s in saved]}
