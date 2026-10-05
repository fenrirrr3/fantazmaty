# -*- coding: utf-8 -*-
"""Nadpisuje wyłącznie gatunek i tagi 17 istniejących tekstów. Uruchom przez manage.py shell."""

ROWS = [{'anthology': 'Raz jeszcze w wyłom',
  'title': 'Kolor piekła',
  'genre': 'humorystyczna science fiction, satyra',
  'tags': 'konsumpcjonizm, chciwość, nierówności społeczne, religia, wojna, przyjaźń, poświęcenie, trauma'},
 {'anthology': 'Raz jeszcze w wyłom',
  'title': 'Wyrywając dusze',
  'genre': 'steampunk, science fantasy',
  'tags': 'wojna, nieśmiertelność, klony, transfer świadomości, tożsamość, fanatyzm, propaganda, zdrada'},
 {'anthology': 'Raz jeszcze w wyłom',
  'title': 'Władcy Ludzkich Ciał',
  'genre': 'fantasy przygodowe, dark fantasy',
  'tags': 'niewola, wolność, bunt, magia, zaklęta broń, latający okręt, zdrada, zakładnicy'},
 {'anthology': 'Raz jeszcze w wyłom',
  'title': 'Kluczarnia',
  'genre': 'groteska fantastyczna, satyra',
  'tags': 'szkoła, biurokracja, bunt, manipulacja, konformizm, kontrola społeczna, absurd'},
 {'anthology': 'Raz jeszcze w wyłom',
  'title': 'Biała brama',
  'genre': 'fantasy metafizyczne, dark fantasy',
  'tags': 'wojna, utrata pamięci, tożsamość, przyjaźń, poświęcenie, przemiana, imiona, portale'},
 {'anthology': 'Raz jeszcze w wyłom',
  'title': 'Słońce Austerlitz',
  'genre': 'fantasy historyczne, groza',
  'tags': 'wojny napoleońskie, postaci historyczne, telepatia, nadprzyrodzone zdolności, potwory, zamach, '
          'tajemnica, przyjaźń'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Wiedźma',
  'genre': 'urban fantasy, dramat psychologiczny',
  'tags': 'wiedźmy, magiczne przedmioty, utrata pamięci, manipulacja, żałoba, depresja, przyjaźń, samotność'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Jenot szuka żony',
  'genre': 'urban fantasy, czarna komedia',
  'tags': 'wiedźmy, demony, nieśmiertelność, nekromancja, zakładnicy, duchy, miłość, czarny humor'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Egzorcyzmy Emily OS',
  'genre': 'science fantasy, groza',
  'tags': 'sztuczna inteligencja, opętanie, demony, religia, kolonializm, kosmici, fanatyzm, śledztwo, '
          'stacja kosmiczna'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Gdzie Diabeł mówi dzień dobry',
  'genre': 'dark fantasy, body horror',
  'tags': 'zaświaty, diabeł, demony, dusza, reinkarnacja, zdrada, moralność, psy'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Klatka z kości',
  'genre': 'fantasy mitologiczne, fantasy metafizyczne',
  'tags': 'szamanizm, duchy, zaświaty, żałoba, poświęcenie, uzdrawianie, wędrówka, przemiana'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Wąż',
  'genre': 'dark fantasy, dystopia',
  'tags': 'narkotyki, wykluczenie, nierówności społeczne, choroba, religia, manipulacja, władza, utrata '
          'człowieczeństwa'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Matcha chocolate chip frappuccino',
  'genre': 'weird fiction, fantastyka filozoficzna',
  'tags': 'tożsamość, pamięć, świadomość, wolna wola, konformizm, media społecznościowe, potwory, sens '
          'życia'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Opowieść o Midasie z Podhala',
  'genre': 'fantasy historyczne, baśń',
  'tags': 'diabeł, klątwa, przemiana ciała, rusałki, spełnianie życzeń, upływ czasu, zakazana wiedza, '
          'muzyka'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Kosmiczna ballada',
  'genre': 'science fantasy, komedia',
  'tags': 'podróż w czasie, statki kosmiczne, muzyka, wojna, przyjaźń, ksenofobia, parodia'},
 {'anthology': 'Fantazmaty 5',
  'title': 'O trzech rycerzach-asemblerach i smoku v1.1.0',
  'genre': 'science fantasy, baśń',
  'tags': 'wirtualna rzeczywistość, programowanie, rycerze, smoki, turniej, władza, zdrada, inwazja'},
 {'anthology': 'Fantazmaty 5',
  'title': 'Cztery miecze Zawiszy',
  'genre': 'przygodowa science fiction, fantastyka historyczna',
  'tags': 'postaci historyczne, średniowiecze, rycerze, transfer świadomości, kosmici, telepatia, ratunek, '
          'honor, zamach'}]

from collections import Counter
from pathlib import Path
import json

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.db import transaction
from texts.models import Anthology, Text


def run_import(*, apply_changes=True, report_path="raport_tagi_wylom_fantazmaty5.json"):
    report = {"mode": "podgląd — nic nie zapisano", "updated": 0, "unchanged": 0, "texts": [], "errors": []}
    try:
        keys = [(row["anthology"], row["title"]) for row in ROWS]
        duplicates = [key for key, count in Counter(keys).items() if count != 1]
        if duplicates:
            raise CommandError(f"Powtórzone pozycje w danych importu: {duplicates}")
        with transaction.atomic():
            plan = []
            for book_title in sorted({row["anthology"] for row in ROWS}):
                books = list(Anthology.objects.select_for_update().filter(title=book_title).order_by("pk"))
                if len(books) != 1 or books[0].title != book_title:
                    report["errors"].append(f"Antologia {book_title!r}: brak jednego rekordu o dokładnie takiej nazwie.")
                    continue
                rows = [row for row in ROWS if row["anthology"] == book_title]
                candidates = list(Text.objects.select_for_update().filter(
                    anthology=books[0], title__in=[row["title"] for row in rows]).order_by("pk"))
                for row in rows:
                    matches = [text for text in candidates if text.title == row["title"]]
                    if len(matches) != 1:
                        report["errors"].append(f"{book_title} / {row['title']}: znaleziono {len(matches)} rekordów zamiast 1.")
                        continue
                    text = matches[0]
                    for field in ("genre", "tags"):
                        Text._meta.get_field(field).clean(row[field], text)
                    changed = (text.genre, text.tags) != (row["genre"], row["tags"])
                    report["texts"].append({"id": text.pk, **row, "changed": changed,
                        "previous_genre": text.genre, "previous_tags": text.tags})
                    plan.append((text, row, changed))
            if report["errors"]:
                raise CommandError("; ".join(report["errors"]))
            for text, row, changed in plan:
                if changed:
                    if apply_changes:
                        text.genre, text.tags = row["genre"], row["tags"]
                        text.save(update_fields=["genre", "tags"])
                    report["updated"] += 1
                else:
                    report["unchanged"] += 1
        report["mode"] = "zapisano" if apply_changes else "podgląd — nic nie zapisano"
    except Exception as error:
        report["mode"] = "przerwano — nic nie zapisano"
        report["updated"] = 0
        if not report["errors"]:
            report["errors"].append(str(error))
        write_report(report, report_path)
        raise CommandError("Nie zapisano zmian: " + str(error)) from error
    write_report(report, report_path)
    verb = "Zmieniono" if apply_changes else "Do zmiany"
    print(f"{verb}: {report['updated']}; już zgodne: {report['unchanged']}. Sprawdzono {len(ROWS)} tekstów.")
    return report


def write_report(report, report_path):
    try:
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Raport: {report_path}")
    except OSError as error:
        print(f"Wynik importu: {report['mode']}. Nie udało się zapisać raportu: {error}")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if globals().get("RUN_IMPORT", True):
    run_import(apply_changes=globals().get("APPLY", True))
