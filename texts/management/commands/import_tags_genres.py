"""Strict, additive import into existing Text.tags and Text.genre."""
import csv
import hashlib
import html
import io
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import get_close_matches
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from core.normalization import compact
from texts.models import Anthology, Text


COLUMNS = ("lp", "text_id", "antologia", "tytul", "tagi", "gatunek")


def merge_values(old, incoming, *, genre=False):
    seen, result = set(), []
    for value in (old, incoming):
        for part in re.split(r"[,\r\n]+", value or ""):
            part = compact(part) if genre else " ".join(part.split())
            if part and part.casefold() not in seen:
                seen.add(part.casefold())
                result.append(part)
    return ", ".join(result)


def read_rows(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter=";", strict=True)
    if reader.fieldnames != list(COLUMNS):
        raise ValueError("Nieprawidłowy nagłówek CSV. Wymagany: " + ";".join(COLUMNS))
    rows = []
    for data in reader:
        if None in data or any(value is None for value in data.values()):
            raise ValueError(f"Nieprawidłowa liczba kolumn: wiersz pliku {reader.line_num}.")
        row = dict(data, file_line=reader.line_num, errors=[], candidates=[])
        for key in ("lp", "text_id"):
            value = data[key]
            if (key == "lp" or value) and not re.fullmatch(r"[1-9][0-9]{0,18}", value):
                row["errors"].append(f"{key}: wymagany dodatni numer całkowity (maks. 19 cyfr).")
        for key in ("tytul", "tagi", "gatunek"):
            if not data[key].strip():
                row["errors"].append(f"Puste pole: {key}.")
        if data["tytul"] != data["tytul"].strip():
            row["errors"].append("Tytuł zawiera skrajne białe znaki. Popraw go świadomie w CSV.")
        if data["antologia"] != data["antologia"].strip():
            row["errors"].append("Antologia zawiera skrajne białe znaki.")
        rows.append(row)
    if not rows:
        raise ValueError("Plik CSV nie zawiera danych.")
    counts = Counter(row["lp"] for row in rows)
    for row in rows:
        if counts[row["lp"]] > 1:
            row["errors"].append("Powtórzony numer lp w CSV.")
    return rows


def candidate(text, anthologies):
    return {"text_id": text.pk, "tytul": text.title,
            "antologia": anthologies.get(text.anthology_id, ""),
            "autorzy": text.authors_display}


def plan_rows(rows, *, lock):
    # Separate queries avoid FOR UPDATE on nullable joins. Lock order is stable.
    queryset = Text.objects.order_by("pk").only("pk", "title", "tags", "genre", "anthology_id")
    if lock:
        queryset = queryset.select_for_update()
    texts = list(queryset.prefetch_related("authors"))
    by_id = {text.pk: text for text in texts}
    by_title = defaultdict(list)
    for text in texts:
        by_title[text.title].append(text)
    anthologies = dict(Anthology.objects.values_list("pk", "title"))
    chosen = {}
    for index, row in enumerate(rows):
        if row["errors"]:
            continue
        matches = by_title.get(row["tytul"], [])
        # Comparison in Python deliberately avoids case-insensitive DB collations.
        if row["text_id"]:
            obj = by_id.get(int(row["text_id"]))
            matches = [obj] if obj else []
            if obj and obj.title != row["tytul"]:
                row["errors"].append("ID wskazuje tekst o innym tytule. ID nie omija kontroli tytułu.")
        row["candidates"] = [candidate(obj, anthologies) for obj in matches]
        if row["antologia"]:
            matches = [obj for obj in matches if anthologies.get(obj.anthology_id) == row["antologia"]]
        if len(matches) != 1:
            row["errors"].append("Brak dokładnego dopasowania tytułu i wskazanych danych." if not matches
                                 else "Niejednoznaczny tytuł. Uzupełnij text_id lub antologia w CSV.")
            if not row["candidates"]:
                similar = get_close_matches(row["tytul"], list(by_title), n=5, cutoff=0.55)
                row["suggestions_only"] = [candidate(obj, anthologies) for title in similar for obj in by_title[title]]
            continue
        if row["errors"]:
            continue
        obj = matches[0]
        row["matched"] = candidate(obj, anthologies)
        chosen[index] = obj
    targets = defaultdict(list)
    for index, obj in chosen.items():
        targets[obj.pk].append(index)
    for indexes in targets.values():
        if len(indexes) > 1:
            for index in indexes:
                rows[index]["errors"].append("Więcej niż jeden wiersz CSV wskazuje ten sam tekst. Import przerwany.")
    plan = []
    for index, obj in chosen.items():
        row = rows[index]
        row.update(old_tags=obj.tags, old_genre=obj.genre,
                   new_tags=merge_values(obj.tags, row["tagi"]),
                   new_genre=merge_values(obj.genre, row["gatunek"], genre=True))
        for field, key in (("tags", "new_tags"), ("genre", "new_genre")):
            try:
                model_field = Text._meta.get_field(field)
                if model_field.max_length and len(row[key]) > model_field.max_length:
                    raise ValidationError(f"Wynik ma {len(row[key])} znaków; limit pola wynosi {model_field.max_length}.")
                model_field.clean(row[key], obj)
            except ValidationError as error:
                row["errors"].append(f"{field}: " + "; ".join(error.messages))
        row["changed_fields"] = [field for field, old, new in (
            ("Text.tags", row["old_tags"], row["new_tags"]),
            ("Text.genre", row["old_genre"], row["new_genre"])) if old != new]
        plan.append((row, obj))
    return plan


def html_report(report):
    def esc(value):
        return html.escape(str(value), quote=True)
    body = []
    for row in report["rows"]:
        match = row.get("matched", {})
        errors = row["errors"]
        status = "BŁĄD" if errors else ("Zapisano" if report["status"] == "ZAPISANO" else "Plan / bez zapisu")
        if not errors and not row.get("changed_fields"):
            status = "Bez zmian" if report["status"] in ("ZAPISANO", "KONTROLA_OK") else "Bez zapisu"
        details = "<br>".join(esc(value) for value in errors)
        suggestions = row.get("suggestions_only", [])
        if not match:
            for value in row.get("candidates", []) + suggestions:
                details += "<p>" + ("Sugestia, NIE dopasowanie: " if value in suggestions else "Kandydat: ")
                details += esc(f'ID {value["text_id"]} – {value["tytul"]} – {value["antologia"]} – {value["autorzy"]}') + "</p>"
        metadata = esc(f'ID {match.get("text_id", "–")} · {match.get("antologia", row["antologia"])} · {match.get("autorzy", "")}')
        def delta(kind, incoming):
            return ("<small>Przed:</small><div>" + esc(row.get("old_" + kind, "–")) + "</div>"
                    + "<small>W pliku:</small><div>" + esc(row[incoming]) + "</div>"
                    + "<small>Po scaleniu (plan):</small><div>" + esc(row.get("new_" + kind, "–")) + "</div>")
        body.append(f'<tr class="{"error" if errors else ""}"><td>{esc(row["lp"])}</td><td><strong>{esc(row["tytul"])}</strong><p>{metadata}</p></td>'
                    f'<td>{delta("tags", "tagi")}</td><td>{delta("genre", "gatunek")}</td><td><strong>{esc(status)}</strong><p>{details}</p></td></tr>')
    summary = report.get("summary", {})
    return f'''<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Raport importu tagów i gatunków</title><style>
body{{font:15px/1.5 system-ui,sans-serif;background:#f3f6fa;color:#1b2938;margin:0;padding:28px}}main{{max-width:1600px;margin:auto}}
h1{{font-size:28px}}.box{{background:white;padding:20px;border-radius:12px;margin-bottom:20px}}.scroll{{overflow:auto}}
table{{border-collapse:collapse;width:100%;background:white;min-width:1000px}}th,td{{text-align:left;vertical-align:top;padding:14px;border-bottom:1px solid #dce3ed}}
th{{background:#19384b;color:white}}td:nth-child(3){{width:30%}}td:nth-child(4){{width:22%}}p{{margin:.6em 0}}small{{color:#536778}}
.error{{background:#fff0ee}}code{{overflow-wrap:anywhere}}@media print{{body{{padding:0}}.scroll{{overflow:visible}}table{{min-width:0;font-size:10px}}}}
</style><main><h1>Import tagów i gatunków</h1><section class="box"><p><strong>{esc(report["status"])}</strong> · {esc(report["time"])}</p>
<p>{esc(report.get("message", ""))}</p><p>Wiersze: {len(report["rows"])} · Błędne: {summary.get("errors", 0)} · Planowane zmiany tekstów: {summary.get("planned", 0)} · Zapisane teksty: {summary.get("written", 0)}</p>
<p>Dopisywanie bez powtórzeń. Dotychczasowe tagi i gatunki są zachowane. Tytuły porównywane dokładnie, bez automatycznej korekty. Kolumny „Po scaleniu” przedstawiają plan; zapis potwierdza wyłącznie status ZAPISANO.</p>
<p>CSV: <code>{esc(report.get("input", ""))}</code><br>SHA-256: <code>{esc(report.get("sha256", ""))}</code></p></section>
<div class="scroll"><table><thead><tr><th>LP</th><th>Opowiadanie</th><th>Tagi</th><th>Gatunek</th><th>Wynik / uwagi</th></tr></thead><tbody>{"".join(body)}</tbody></table></div></main></html>'''


def write_report(path, report):
    # Reuse only paths reserved by this run; publish each complete file atomically.
    for target, content in ((path.with_suffix(".json"), json.dumps(report, ensure_ascii=False, indent=2)),
                            (path, html_report(report))):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=target.parent,
                                             prefix=target.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
            os.replace(temporary, target)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()


class Command(BaseCommand):
    help = "Kontrola i atomowe dopisanie tagów / gatunków do istniejących opowiadań (domyślnie bez zapisu)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="CSV UTF-8, separator średnik.")
        parser.add_argument("--apply", action="store_true", help="Zapisz wszystkie dane, jeśli cały plik przejdzie kontrolę.")
        parser.add_argument("--report", help="Nowy plik HTML; obok powstanie JSON. Istniejące raporty nie są nadpisywane.")

    def handle(self, *args, **options):
        source = Path(options["csv_file"]).resolve()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = Path(options["report"] or f"raport_importu_{stamp}.html").resolve()
        if path.suffix.lower() != ".html":
            raise CommandError("Raport musi mieć rozszerzenie .html.")
        targets = (path, path.with_suffix(".json"))
        if source in targets:
            raise CommandError("Raport nie może zastąpić pliku wejściowego.")
        reserved = []
        try:
            for target in targets:
                with target.open("x", encoding="utf-8"):
                    pass
                reserved.append(target)
        except OSError as error:
            for target in reserved:
                target.unlink()
            raise CommandError(f"Nie można utworzyć raportu; baza nietknięta: {error}") from error
        report = {"status": "ROZPOCZĘTO", "time": datetime.now(timezone.utc).isoformat(),
                  "input": str(source), "apply": options["apply"], "rows": [], "summary": {"written": 0}}
        committed = False
        try:
            write_report(path, report)
            raw = source.read_bytes()
            report["sha256"] = hashlib.sha256(raw).hexdigest()
            report["rows"] = read_rows(raw)
            # Do not offer a false atomicity guarantee on nontransactional MySQL tables.
            if not connection.features.supports_transactions:
                raise ValueError("Silnik bazy nie obsługuje transakcji. Import wyłączony.")
            if options["apply"] and connection.vendor == "mysql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'")
                    invalid = [name for name, engine in cursor.fetchall() if (engine or "").upper() != "INNODB"]
                if invalid:
                    raise ValueError("Zapis wymaga tabel InnoDB; znaleziono inne silniki: " + ", ".join(invalid))
            with transaction.atomic():
                plan = plan_rows(report["rows"], lock=options["apply"])
                errors = sum(bool(row["errors"]) for row in report["rows"])
                planned = sum(bool(row.get("changed_fields")) and not row["errors"] for row in report["rows"])
                report["summary"].update(errors=errors, planned=planned)
                if errors:
                    report.update(status="PRZERWANO", message="Kontrola wykryła rozbieżności. Nie zapisano żadnych zmian. Popraw wskazane wiersze i uruchom kontrolę ponownie.")
                elif options["apply"]:
                    for row, obj in plan:
                        fields = []
                        if "Text.tags" in row["changed_fields"]:
                            obj.tags = row["new_tags"]
                            fields.append("tags")
                        if "Text.genre" in row["changed_fields"]:
                            obj.genre = row["new_genre"]
                            fields.append("genre")
                        if fields:
                            obj.save(update_fields=fields)
                    # Report filesystem failures here still roll back the transaction.
                    report.update(status="OCZEKUJE_NA_COMMIT", message="Zapisy wykonane w transakcji; brak potwierdzenia jej zatwierdzenia. Nie jest to raport sukcesu.")
                    write_report(path, report)
                else:
                    report.update(status="KONTROLA_OK", message="Pełna kontrola zakończona. Baza nie została zmieniona. Opcja --apply ponownie sprawdzi dane i wykona zapis.")
            if not errors and options["apply"]:
                committed = True
                report["summary"]["written"] = planned
                report.update(status="ZAPISANO", message="Transakcja zatwierdzona. Dopisano wartości do istniejących pól. Nie tworzono tekstów ani zgłoszeń.")
        except Exception as error:
            report.update(status="PRZERWANO", message=f"Nie zapisano zmian; transakcja (jeśli rozpoczęta) wycofana. {type(error).__name__}: {error}")
            report["summary"]["errors"] = sum(bool(row["errors"]) for row in report["rows"])
        try:
            write_report(path, report)
        except OSError as error:
            state = "IMPORT ZAPISANY W BAZIE" if committed else "BRAK ZAPISU W BAZIE"
            self.stderr.write(json.dumps(report, ensure_ascii=False, indent=2))
            raise CommandError(f"{state}, ale zapis końcowego raportu nie powiódł się: {error}. Raport wypisano w konsoli.") from error
        self.stdout.write(f'{report["status"]}: {report["message"]}')
        self.stdout.write(f'Raport HTML: {path}\nRaport JSON: {path.with_suffix(".json")}')
        if report["status"] == "PRZERWANO":
            raise CommandError("Import przerwany bez zmian. Szczegóły w raporcie.")
