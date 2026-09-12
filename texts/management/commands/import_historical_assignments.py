import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from people.models import Person
from texts.models import HistoricalTextAssignment, Text


class Command(BaseCommand):
    help = "Import historycznych udziałów z JSON. Domyślnie tylko podgląd; zapis wymaga --commit."

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument("--commit", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            records = json.loads(Path(options["file"]).read_text(encoding="utf-8-sig"))
            if not isinstance(records, list) or not records:
                raise ValueError("Plik musi zawierać niepustą listę rekordów.")
            created = 0
            skipped = 0
            for index, record in enumerate(records, 1):
                if not isinstance(record, dict):
                    raise ValueError(f"Rekord {index}: wymagany obiekt JSON.")
                allowed = {"text_id", "person_id", "person_name", "role", "position", "notes", "started_at", "ended_at", "is_completed", "participant", "source_row", "source_status"}
                if set(record) - allowed:
                    raise ValueError(f"Rekord {index}: nieznane pola: {sorted(set(record) - allowed)}")
                for key in ("text_id", "position"):
                    if type(record.get(key)) is not int or record[key] < 1:
                        raise ValueError(f"Rekord {index}: {key} musi być dodatnią liczbą całkowitą.")
                record = dict(record)
                record.setdefault("participant", 1)
                for key in ("participant", "source_row"):
                    if record.get(key) is not None and (type(record[key]) is not int or record[key] < 1):
                        raise ValueError(f"Rekord {index}: {key} musi być dodatnią liczbą całkowitą.")
                person_id = record.get("person_id")
                if person_id is not None and (type(person_id) is not int or person_id < 1):
                    raise ValueError(f"Rekord {index}: nieprawidłowe person_id.")
                text = Text.objects.select_for_update().get(pk=record["text_id"])
                person = Person.objects.get(pk=person_id) if person_id is not None else None
                data = {key: record[key] for key in allowed - {"text_id", "person_id"} if key in record}
                for key in ("person_name", "role", "notes"):
                    if key in data and not isinstance(data[key], str):
                        raise ValueError(f"Rekord {index}: {key} musi być tekstem.")
                if not data.get("person_name", "").strip():
                    data["person_name"] = str(person) if person else ""
                if "is_completed" in data and type(data["is_completed"]) is not bool:
                    raise ValueError(f"Rekord {index}: is_completed musi być booleanem.")
                for key in ("started_at", "ended_at"):
                    if data.get(key):
                        raise ValueError(f"Rekord {index}: historia nie zawiera dat; {key} musi pozostać puste.")
                    elif key in data:
                        data[key] = None
                item = HistoricalTextAssignment(text=text, person=person, **data)
                # The flag and records are committed or rolled back together.
                if not text.is_historical:
                    text.is_historical = True
                    text.save(update_fields=["is_historical"])
                existing = HistoricalTextAssignment.objects.filter(text=text, role=item.role, position=item.position, participant=item.participant).first()
                if existing:
                    fields = ("person_id", "person_name", "notes", "started_at", "ended_at", "is_completed", "source_row", "source_status")
                    if any(getattr(existing, field) != getattr(item, field) for field in fields):
                        raise ValueError(f"Rekord {index}: istniejący udział różni się od importowanego; historia nie została nadpisana.")
                    skipped += 1
                    continue
                item.full_clean()
                item.save()
                created += 1
            if not options["commit"]:
                transaction.set_rollback(True)
            mode = "Zapisano" if options["commit"] else "Podgląd bez zapisu"
            self.stdout.write(self.style.SUCCESS(f"{mode}: {created} nowych, {skipped} już istniejących."))
        except (ValueError, TypeError, OSError, Text.DoesNotExist, Person.DoesNotExist, ValidationError) as exc:
            raise CommandError(str(exc)) from exc
