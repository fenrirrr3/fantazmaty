"""Idempotent, transactional bridge from accepted Extract titles to volumes."""

import hashlib
import json
import unicodedata
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from people.models import Person
from texts.extract_data import split_list
from texts.models import (
    Anthology,
    AnthologyTask,
    Extract,
    ExtractTextLink,
    ExtractVolume,
    ExtractVolumeCredit,
    Text,
)
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage


def key(value):
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


class Command(BaseCommand):
    help = "Tworzy tomy Ekstraktów i przyjęte miniatury. Domyślnie podgląd; zapis: --apply."

    def add_arguments(self, parser):
        parser.add_argument("input")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--report", default="raport_ekstrakty.json")
        parser.add_argument(
            "--map",
            dest="mapping",
            help="JSON: people {podpis: ID profilu Person}, recruitments {nabór: numer tomu}.",
        )
        parser.add_argument(
            "--create-missing-people",
            action="store_true",
            help="Dodaj brakujące nieaktywne profile bez kont i e-maili. Nigdy nie rozstrzyga duplikatów.",
        )

    def handle(self, *args, **options):
        try:
            raw = Path(options["input"]).read_bytes()
            payload = json.loads(raw.decode("utf-8-sig"))
            volumes = payload["volumes"]
            if payload.get("schema") != "extract-volumes-v1" or [v["number"] for v in volumes] != [
                1,
                2,
                3,
            ]:
                raise ValueError("Wymagane tomy 1, 2 i 3, schema extract-volumes-v1.")
            mapping = (
                json.loads(Path(options["mapping"]).read_text(encoding="utf-8-sig"))
                if options["mapping"]
                else {}
            )
            if not isinstance(mapping, dict) or any(
                not isinstance(mapping.get(k, {}), dict) for k in ("people", "recruitments")
            ):
                raise ValueError("Nieprawidłowe mapowanie.")
            for volume in volumes:
                for credit in volume["credits"]:
                    if any(
                        not isinstance(credit.get(k), str) or not credit[k].strip()
                        for k in ("role", "name")
                    ):
                        raise ValueError("Nieprawidłowy podpis w stopce.")
                    if len(credit["role"]) > 100 or len(credit["name"]) > 255:
                        raise ValueError("Podpis przekracza długość pola.")
                if volume["number"] == 3 and volume["credits"]:
                    raise ValueError("Tom 3 nie ma jeszcze potwierdzonej stopki.")
            report_path = Path(options["report"])
            if report_path.resolve() in {
                Path(options["input"]).resolve(),
                Path(options["mapping"]).resolve() if options["mapping"] else None,
            }:
                raise ValueError("Raport nie może nadpisać danych lub mapowania.")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CommandError(str(exc)) from exc
        report = {
            "mode": "podgląd – nic nie zapisano",
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "volumes": [],
            "people": [],
            "texts": [],
            "conflicts": [],
            "warnings": [],
            "notes": "Tylko przyjęte miniatury. Źródłowe Ekstrakty, role i konta pozostają bez zmian. Daty i nieznane długości pozostają puste.",
        }
        with transaction.atomic():
            try:
                # A savepoint allows writing a conflict report even after a validation error.
                with transaction.atomic():
                    self.import_all(volumes, mapping, options, report)
            except (ValueError, ValidationError) as exc:
                report["conflicts"].append(str(exc))
            if report["conflicts"]:
                report["mode"] = "przerwano – nic nie zapisano"
            elif options["apply"]:
                report["mode"] = "zapisano"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if report["conflicts"] or not options["apply"]:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if report["conflicts"]:
            raise CommandError(f"Nie zapisano zmian. Szczegóły: {report_path}")

    def resolve_people(self, volumes, mapping, options, report):
        people = list(Person.objects.select_for_update().order_by("pk"))
        users = list(get_user_model().objects.order_by("pk"))
        result = {}
        for name in dict.fromkeys(c["name"] for v in volumes for c in v["credits"]):
            person_id = mapping.get("people", {}).get(name)
            matches = [
                p
                for p in people
                if (p.pk == person_id if person_id is not None else key(str(p)) == key(name))
            ]
            if len(matches) == 1:
                person = matches[0]
                action = "istniejący profil"
            elif not matches and person_id is None and options["create_missing_people"]:
                accounts = [u for u in users if key(u.get_full_name()) == key(name)]
                if len(accounts) > 1 or any(p.user_id in {u.pk for u in accounts} for p in people):
                    report["conflicts"].append(
                        f"{name}: istnieje konto lub profil z inną nazwą; podaj people w --map (ID profilu)."
                    )
                    continue
                first, sep, last = name.strip().partition(" ")
                if not sep:
                    report["conflicts"].append(f"{name}: wymagane jawne mapowanie profilu.")
                    continue
                person = Person(
                    first_name=first,
                    last_name=last,
                    is_active=False,
                    email=None,
                    user=accounts[0] if accounts else None,
                )
                person.full_clean()
                person.save()
                people.append(person)
                action = (
                    "nowy nieaktywny profil; istniejące konto"
                    if accounts
                    else "nowy nieaktywny profil bez konta"
                )
            else:
                report["conflicts"].append(
                    f"{name}: znaleziono {len(matches)} profili. Użyj --map lub (przy braku osoby) --create-missing-people."
                )
                continue
            result[name] = person
            report["people"].append(
                {"source_name": name, "person_id": person.pk, "name": str(person), "action": action}
            )
        return result

    def import_all(self, volumes, mapping, options, report):
        aliases = {
            "ekstrakty": 1,
            "ekstrakty 1": 1,
            "ekstrakty i": 1,
            "ekstrakty 2": 2,
            "ekstrakty ii": 2,
            "ekstrakty 3": 3,
            "ekstrakty iii": 3,
        }
        for label, number in mapping.get("recruitments", {}).items():
            if number not in (1, 2, 3):
                raise ValueError(f"Nieprawidłowy numer tomu: {label}.")
            aliases[key(label)] = number
        sources = {1: [], 2: [], 3: []}
        for source in Extract.objects.select_for_update().select_related("author").order_by("pk"):
            number = aliases.get(key(source.recruitment))
            if number is None:
                report["conflicts"].append(
                    f"Nieznany nabór „{source.recruitment}”. Przypisz numer tomu przez recruitments w --map."
                )
                continue
            source.prepare_lists()
            sources[number].append(source)
        if not any(sources.values()):
            report["conflicts"].append("Nie znaleziono źródłowych rekordów Ekstraktów.")
        people = self.resolve_people(volumes, mapping, options, report)
        if report["conflicts"]:
            return
        # Fixed order and locks serialize reruns that share existing source records.
        for volume in volumes:
            number = volume["number"]
            title = f"Ekstrakty {number}"
            marker = (
                ExtractVolume.objects.select_for_update()
                .filter(number=number)
                .select_related("anthology")
                .first()
            )
            if marker:
                book = Anthology.objects.select_for_update().get(pk=marker.anthology_id)
                if book.is_novel or book.is_translated:
                    raise ValueError(f"{title}: zmieniony rodzaj publikacji.")
            else:
                existing = [
                    b
                    for b in Anthology.objects.select_for_update().all()
                    if key(b.title) == key(title)
                ]
                if existing:
                    raise ValueError(
                        f"{title}: istnieje już antologia bez powiązania importu. Nie tworzę duplikatu ani nie przejmuję jej automatycznie."
                    )
                book = Anthology.objects.create(title=title)
                marker = ExtractVolume.objects.create(anthology=book, number=number)
            book_row = {
                "number": number,
                "anthology_id": book.pk,
                "title": book.title,
                "source_records": len(sources[number]),
                "accepted_titles": 0,
                "new_texts": 0,
                "unchanged_texts": 0,
            }
            report["volumes"].append(book_row)
            seen_pairs = set()
            accepted_links = set()
            for source in sources[number]:
                for title in split_list(source.accepted_titles):
                    if len(title) > 255:
                        raise ValueError(f"Zbyt długi tytuł w Ekstrakty #{source.pk}.")
                    title_key = key(title)
                    pair = (source.author_id, title_key)
                    if pair in seen_pairs:
                        raise ValueError(
                            f"Powtórzony przyjęty tytuł autora w tomie: {title}. Sprawdź źródłowe Ekstrakty."
                        )
                    seen_pairs.add(pair)
                    book_row["accepted_titles"] += 1
                    link = (
                        ExtractTextLink.objects.select_for_update()
                        .filter(extract=source, title_key=title_key)
                        .select_related("text")
                        .first()
                    )
                    if link:
                        text = link.text
                        if (
                            text.anthology_id != book.pk
                            or source.author_id not in text.authors.values_list("pk", flat=True)
                        ):
                            raise ValueError(
                                f"{title}: zmieniono antologię lub autora wcześniej importowanej miniatury."
                            )
                        action = "bez zmian"
                        book_row["unchanged_texts"] += 1
                    else:
                        if any(
                            key(t.title) == title_key
                            for t in Text.objects.filter(authors=source.author, anthology=book)
                        ):
                            raise ValueError(
                                f"{title}: istnieje już tekst autora bez powiązania importu; nie dubluję."
                            )
                        text = Text(title=title, import_source="extracts-v1", length=None)
                        text.full_clean()
                        text.save()
                        text.authors.add(source.author)
                        token = importing_completed.set(True)
                        try:
                            WorkflowStage.objects.create(
                                text=text,
                                stage_type="ready" if number < 3 else "ready_for_editing",
                                is_current=True,
                                is_released=True,
                            )
                        finally:
                            importing_completed.reset(token)
                        text.anthology = book
                        text.full_clean()
                        text.save(update_fields=["anthology"])
                        link = ExtractTextLink.objects.create(
                            extract=source, text=text, title_key=title_key, source_title=title
                        )
                        action = "dodanie"
                        book_row["new_texts"] += 1
                    accepted_links.add(link.pk)
                    report["texts"].append(
                        {
                            "extract_id": source.pk,
                            "text_id": text.pk,
                            "title": text.title,
                            "author_id": source.author_id,
                            "author": source.author.display_name,
                            "anthology": book.title,
                            "action": action,
                        }
                    )
            removed = ExtractTextLink.objects.filter(text__anthology=book).exclude(
                pk__in=accepted_links
            )
            if removed.exists():
                raise ValueError(
                    f"{book.title}: wcześniejsze miniatury zniknęły z przyjętych tytułów. Nie usuwam ich automatycznie; sprawdź źródło."
                )
            if not book_row["accepted_titles"]:
                report["warnings"].append(
                    f"{book.title}: brak przyjętych tytułów w źródłowej bazie."
                )
            for position, credit in enumerate(volume["credits"]):
                person = people[credit["name"]]
                ExtractVolumeCredit.objects.get_or_create(
                    anthology=book,
                    person=person,
                    role=credit["role"],
                    defaults={
                        "source_name": credit.get("original_name", credit["name"]),
                        "position": position,
                    },
                )
                task_type = credit.get("task")
                if task_type:
                    if task_type not in dict(AnthologyTask.TaskType.choices):
                        raise ValueError(f"Nieznany rodzaj zadania: {task_type}.")
                    task = AnthologyTask.objects.select_for_update().get(
                        anthology=book, task_type=task_type
                    )
                    if task.assigned_to_id and (
                        task.assigned_to_id != person.pk or task.status != "ready"
                    ):
                        raise ValueError(
                            f"{book.title}: zadanie {task.get_task_type_display()} ma inne dane. Nie nadpisuję."
                        )
                    if not task.assigned_to_id:
                        task.assigned_to = person
                        task.status = "ready"
                        task.full_clean()
                        task.save()
                if credit.get("cover"):
                    if book.cover_author and key(book.cover_author) != key(str(person)):
                        raise ValueError(f"{book.title}: inny autor okładki w bazie.")
                    book.cover_author = str(person)
                    book.cover_status = "ready"
            if number < 3:
                book.status = "ready"
            book.full_clean()
            book.save()
            book_row["status"] = book.get_status_display()
            book_row["credits"] = book.extract_credits.count()
