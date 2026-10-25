#!/usr/bin/env python3
"""Wybór testów według zmienionych plików (czysty Python 3.12+, bez Django; Windows i Linux).

Użycie:
    python tools/ci/affected.py --base origin/main       # zmiany od wspólnego przodka z origin/main
    python tools/ci/affected.py --base HEAD~1 --fast     # podpowiedź bez testów slow/migration/docx
    python tools/ci/affected.py --files core/static/core/pages.css README.md
    git diff --name-only HEAD~3 | python tools/ci/affected.py --files -
    python tools/ci/affected.py --base origin/main --json
    python tools/ci/affected.py --all                    # pełny plan (harmonogram, uruchomienie ręczne)
    python tools/ci/affected.py --base "$BASE" --github  # CI: wyjścia do $GITHUB_OUTPUT i podsumowanie

Z ``--base`` liczą się zatwierdzone i niezatwierdzone zmiany (także nowe, niedodane pliki), a heurystyka
SQL ogląda tylko zmienione linie. Z ``--files`` heurystyka ogląda całe pliki (ostrożniej).

Zasada: każdy zmieniony plik trafia do PIERWSZEJ pasującej reguły z tabeli RULES. Plik bez reguły
uruchamia pełną suitę SQLite. MySQL dochodzi, gdy zmiana dotyka modeli, migracji, selektorów, ustawień
(reguły MYSQL) albo gdy zmienione linie pliku .py zawierają konstrukcje kształtujące SQL (SQL_MARKERS).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Rodzaje reguł.
IGNORE = "ignore"  # bez testów
TESTS = "tests"  # testy jednego pakietu – etykieta z katalogu
AREA = "area"  # pakiety testów obszaru
MYSQL = "mysql"  # pełna suita SQLite + MySQL
FULL = "full"  # pełna suita SQLite

PROGRAMS = ("core.tests.programs", "core.tests.recruitment")  # import ze skrzynki używa przebudowy DOCX
UI = ("core.tests.ui",)
FAST_EXCLUDE_TAGS = ("slow", "migration", "docx")
RELEASE_LABELS = "core.tests.ui core.tests.texts core.tests.security"
STYLELINT = 'npx --yes stylelint@16.10.0 "core/static/core/*.css"'
PHP_TEST = "php tools/wordpress/test-snippets.php"


@dataclass(frozen=True)
class Rule:
    name: str
    patterns: tuple[str, ...]
    kind: str
    labels: tuple[str, ...] = ()


# Wzorce: `*` i `?` nie obejmują `/`, `**` obejmuje dowolną liczbę katalogów. Kolejność ma znaczenie.
RULES: tuple[Rule, ...] = (
    # Nic do testowania.
    Rule("dokumentacja i dane", ("*.md", "**/*.md", "docs/**", "LICENSE", ".env.example", "import_data/**",
                                 ".gitignore", ".gitattributes", ".python-version"), IGNORE),
    Rule("narzędzia CI (ich testy uruchamia zadanie lint)", ("tools/ci/**",), IGNORE),
    # Zmiana tylko w testach jednego pakietu → ten pakiet.
    Rule("testy jednego pakietu", ("core/tests/*/**", "core/tests/test_*.py", "workflow/tests/**",
                                   "illustrations/tests/**"), TESTS),
    Rule("wspólne pomocniki testów", ("fantazmaty/testing/**", "core/tests/__init__.py"), FULL),
    # Obszary o wyraźnej granicy.
    Rule("CSS", ("core/static/core/*.css", ".stylelintrc.json"), AREA, UI),
    Rule("programy dokumentów – skrypty", ("core/static/core/program-*.js", "core/static/core/odkurzacz.js"),
         AREA, PROGRAMS + UI),
    Rule("JS", ("core/static/core/*.js", "core/js_tests/**"), AREA, UI),
    Rule("pozostałe pliki statyczne", ("core/static/**", "texts/static/**"), AREA, UI),
    Rule("snippety WordPressa", ("tools/wordpress/**",), AREA, ("core.tests.site",)),
    Rule("programy dokumentów", ("core/services/document_*.py", "core/services/odkurzacz.py",
                                 "core/services/program_*.py", "core/services/fonts/**",
                                 "core/odkurzacz_forms.py", "core/program_models.py",
                                 "core/views/programs.py", "core/views/program_jobs.py",
                                 "core/views/replacements.py", "core/templates/core/programs*.html",
                                 "core/templates/core/program_job.html",
                                 "core/templates/core/replacement*.html",
                                 "core/templates/core/includes/program_*.html",
                                 "core/templates/core/includes/replacement_*.html",
                                 "tools/redaktor_docx/**"), AREA, PROGRAMS),
    Rule("strona WWW (ekipa, szukajka, Słuchaj)", ("core/views/site_www.py", "core/site_*.py", "core/site_*.json",
                                                   "core/templates/core/site_*.html"), AREA, ("core.tests.site",)),
    Rule("social media", ("core/views/social_media.py", "core/social_*.py",
                          "core/templates/core/social_media.html"), AREA, ("core.tests.social",)),
    # Baza danych: pełna suita SQLite i MySQL.
    Rule("modele, migracje i selektory", ("*/models.py", "*/*_models.py", "*/migrations/**", "*/selectors/**"),
         MYSQL),
    Rule("zapytania SQL, ustawienia i zależności", ("workflow/**", "core/table_sorting.py", "core/sort_keys.py",
                                                    "core/search_lookup.py", "core/middleware.py",
                                                    "core/activity*.py", "core/supervision.py",
                                                    "core/services/texts.py", "core/services/vacations.py",
                                                    "core/services/reviews.py", "fantazmaty/settings*.py",
                                                    "fantazmaty/*_settings.py", "requirements*.txt"), MYSQL),
    # Szersze obszary (po regułach bazy, żeby modele i migracje poszły na MySQL).
    Rule("ilustracje", ("illustrations/**",), AREA, ("illustrations",) + UI),
)
UNKNOWN = "nierozpoznane → pełna suita"

# Konstrukcje, których zachowanie różni się między SQLite a MySQL (adnotacje, podzapytania, funkcje SQL,
# blokady, wyszukiwanie po datach, porównania ciągów).
SQL_MARKERS = re.compile(
    r"annotate\(|aggregate\(|Subquery|OuterRef|Exists\(|Case\(|When\(|Cast\(|Coalesce\(|RawSQL|\.extra\(|"
    r"Window\(|select_for_update|connection\.cursor|__date|__week|__iso|TruncDate|Collate"
)
# Testy, pomocniki testów, migracje (i tak MySQL) i narzędzia nie są oglądane heurystyką.
SQL_CHECK_EXEMPT = ("**/tests/**", "**/tests.py", "**/test_*.py", "fantazmaty/testing/**", "tools/**",
                    "**/migrations/**")

CSS_FILES = ("core/static/core/*.css", ".stylelintrc.json")
JS_FILES = ("core/static/core/*.js", "core/js_tests/**")
PHP_FILES = ("tools/wordpress/**",)
RELEASE_FILES = ("**/templates/**", "**/static/**")  # collectstatic z manifestem sprawdza url() i {% static %}
DEPENDENCY_FILES = ("requirements*.txt",)
RUFF_FILES = ("*.py", "**/*.py", "pyproject.toml")


# --- dopasowanie ścieżek -----------------------------------------------------------------------------------------

def glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            parts.append(".*")
            i += 2
            continue
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        i += 1
    return re.compile("".join(parts) + r"\Z")


_REGEX_CACHE: dict[str, re.Pattern[str]] = {}


def matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    for pattern in patterns:
        regex = _REGEX_CACHE.get(pattern)
        if regex is None:
            regex = _REGEX_CACHE[pattern] = glob_to_regex(pattern)
        if regex.match(path):
            return True
    return False


def normalize(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def match_rule(path: str) -> Rule | None:
    for rule in RULES:
        if matches(path, rule.patterns):
            return rule
    return None


def test_label(path: str) -> str:
    """Etykieta pakietu testów dla pliku w katalogu testów: core/tests/site/… → core.tests.site."""
    parts = path.split("/")
    if parts[0] == "core":
        if len(parts) > 3:
            return "core.tests." + parts[2]
        return "core.tests." + parts[2].removesuffix(".py")
    return parts[0]


# --- plan ---------------------------------------------------------------------------------------------------------

@dataclass
class Plan:
    base: str | None = None
    files: list[dict[str, str]] = field(default_factory=list)
    sqlite: str = "none"  # full | affected | none
    labels: list[str] = field(default_factory=list)  # puste przy pełnej suicie
    mysql: bool = False
    mysql_reasons: list[str] = field(default_factory=list)
    release: bool = False
    css: bool = False
    js: bool = False
    php: bool = False
    ruff: bool = False
    dependencies: bool = False
    exclude_tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


def full_plan(note: str, *, fast: bool = False) -> Plan:
    plan = Plan(sqlite="full", mysql=True, release=True, css=True, js=True, php=True, ruff=True,
                dependencies=True, notes=[note])
    plan.exclude_tags = list(FAST_EXCLUDE_TAGS) if fast else []
    plan.commands = commands_for(plan)
    return plan


def build_plan(paths: list[str], sql_text: Callable[[str], str], *, fast: bool = False,
               base: str | None = None, sql_scope: str = "zmienione linie") -> Plan:
    """Układa plan dla listy ścieżek; ``sql_text(path)`` zwraca tekst oglądany heurystyką SQL."""
    plan = Plan(base=base)
    labels: set[str] = set()
    full = False
    for path in sorted({normalize(p) for p in paths if p.strip()}):
        rule = match_rule(path)
        kind = rule.kind if rule else FULL
        entry = {"path": path, "rule": rule.name if rule else UNKNOWN, "kind": kind}
        plan.files.append(entry)
        if kind == IGNORE:
            continue
        if kind == TESTS:
            labels.add(test_label(path))
        elif kind == AREA:
            labels.update(rule.labels)
        elif kind == MYSQL:
            full = True
            plan.mysql = True
            plan.mysql_reasons.append(f"{path}: {rule.name}")
        else:
            full = True
        if kind != MYSQL and path.endswith(".py") and not matches(path, SQL_CHECK_EXEMPT):
            hit = SQL_MARKERS.search(sql_text(path))
            if hit:
                full = True
                plan.mysql = True
                plan.mysql_reasons.append(f"{path}: {sql_scope} zawierają „{hit.group()}”")
                entry["sql"] = hit.group()
    changed = [entry["path"] for entry in plan.files]
    plan.css = any(matches(p, CSS_FILES) for p in changed)
    plan.js = any(matches(p, JS_FILES) for p in changed)
    plan.php = any(matches(p, PHP_FILES) for p in changed)
    plan.ruff = any(matches(p, RUFF_FILES) for p in changed)
    plan.dependencies = any(matches(p, DEPENDENCY_FILES) for p in changed)
    plan.release = full or any(matches(p, RELEASE_FILES) for p in changed)
    if full:
        plan.sqlite = "full"
    elif labels:
        plan.sqlite = "affected"
        plan.labels = sorted(labels)
    if fast:
        plan.exclude_tags = list(FAST_EXCLUDE_TAGS)
    plan.commands = commands_for(plan)
    return plan


def commands_for(plan: Plan) -> list[str]:
    commands = []
    if plan.ruff:
        commands.append("ruff check .")
    if plan.css:
        commands.append(STYLELINT)
    if plan.php:
        commands.append(PHP_TEST)
    if plan.sqlite != "none":
        parts = ["python manage.py test --settings=fantazmaty.test_settings", *plan.labels, "--parallel"]
        parts += [f"--exclude-tag {tag}" for tag in plan.exclude_tags]
        commands.append(" ".join(parts))
    if plan.release:
        commands.append("python manage.py collectstatic --noinput --settings=fantazmaty.release_test_settings"
                        f" && python manage.py test --settings=fantazmaty.release_test_settings {RELEASE_LABELS}")
    if plan.mysql:
        commands.append("python manage.py test --settings=fantazmaty.mysql_integration_settings --noinput"
                        "   # wymaga TEST_MYSQL_USER i TEST_MYSQL_PASSWORD")
    return commands


# --- git ----------------------------------------------------------------------------------------------------------

def run_git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def changed_lines_by_file(diff: str) -> dict[str, str]:
    """Z wyniku ``git diff`` zbiera dodane i usunięte linie każdego pliku (bez nagłówków)."""
    lines: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = normalize(line.split(" b/", 1)[1]) if " b/" in line else None
            if current is not None:
                lines.setdefault(current, [])
        elif current is not None and line[:1] in "+-" and not line.startswith(("+++", "---")):
            lines[current].append(line[1:])
    return {path: "\n".join(content) for path, content in lines.items()}


def changes_from_git(base: str) -> tuple[list[str], dict[str, str]] | None:
    """Ścieżki i zmienione linie od wspólnego przodka z ``base`` do drzewa roboczego; None, gdy się nie da."""
    base = base.strip()
    if not base or set(base) <= {"0"}:
        return None
    merge_base = run_git("merge-base", base, "HEAD")
    if merge_base is None:
        return None
    merge_base = merge_base.strip()
    names = run_git("diff", "--name-only", merge_base)
    untracked = run_git("ls-files", "--others", "--exclude-standard")
    diff = run_git("diff", "-U0", "--no-color", merge_base)
    if names is None or untracked is None or diff is None:
        return None
    changed = changed_lines_by_file(diff)
    for path in untracked.splitlines():
        if path.strip():
            changed[normalize(path)] = read_text(normalize(path))
    return [normalize(p) for p in (names + untracked).splitlines() if p.strip()], changed


def read_text(path: str) -> str:
    try:
        return (ROOT / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --- wyjście ------------------------------------------------------------------------------------------------------

def yes_no(value: bool) -> str:
    return "tak" if value else "nie"


def shortened(lines: list[str], limit: int) -> list[str]:
    """Najwyżej ``limit`` linii; resztę zastępuje zdanie z liczbą (pełna lista jest w --json)."""
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"… i {len(lines) - limit} więcej (pełna lista: --json)"]


def describe(plan: Plan) -> str:
    out = []
    source = f", względem {plan.base}" if plan.base else ""
    out.append(f"Zmienione pliki ({len(plan.files)}{source}):")
    width = min(max((len(f["path"]) for f in plan.files), default=0), 60)
    files = []
    for entry in plan.files:
        sql = f" [SQL: {entry['sql']}]" if entry.get("sql") else ""
        files.append(f"  {entry['path']:<{width}}  {entry['rule']}{sql}")
    out.extend(shortened(files, 40))
    out.extend(f"  {note}" for note in plan.notes)
    out.append("")
    out.append("Plan:")
    if plan.sqlite == "full":
        out.append("  SQLite:  pełna suita (CI: Python 3.12 i 3.13)")
    elif plan.sqlite == "affected":
        out.append("  SQLite:  " + " ".join(plan.labels))
    else:
        out.append("  SQLite:  bez testów")
    out.append("  MySQL:   " + yes_no(plan.mysql))
    out.extend(shortened([f"           – {reason}" for reason in plan.mysql_reasons], 10))
    out.append(f"  Kontrola wydania (collectstatic + {RELEASE_LABELS}): {yes_no(plan.release)}")
    out.append(f"  stylelint: {yes_no(plan.css)}   testy JS: {yes_no(plan.js)}   PHP: {yes_no(plan.php)}"
               f"   ruff: {yes_no(plan.ruff)}   pip-audit: {yes_no(plan.dependencies)}")
    if plan.exclude_tags:
        out.append("  Pominięte tagi: " + " ".join(plan.exclude_tags))
    out.append("")
    out.append("Polecenia (Windows i Linux):" if plan.commands else "Nic do uruchomienia.")
    out.extend(f"  {command}" for command in plan.commands)
    return "\n".join(out)


def github_outputs(plan: Plan) -> dict[str, str]:
    flag = {True: "true", False: "false"}
    return {
        "labels": " ".join(plan.labels),
        "sqlite": plan.sqlite,
        "mysql": flag[plan.mysql],
        "release": flag[plan.release],
        "css": flag[plan.css],
        "js": flag[plan.js],
        "php": flag[plan.php],
        "dependencies": flag[plan.dependencies],
        "exclude_tags": " ".join(plan.exclude_tags),
    }


def write_github(plan: Plan) -> None:
    lines = [f"{key}={value}" for key, value in github_outputs(plan).items()]
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### Plan testów\n\n```\n" + describe(plan) + "\n```\n")


def read_files_argument(values: list[str]) -> list[str]:
    paths: list[str] = []
    for value in values:
        if value == "-":
            paths.extend(sys.stdin.read().splitlines())
        else:
            paths.append(value)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wybór testów według zmienionych plików.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base", help="ref gita; zmiany od wspólnego przodka do drzewa roboczego")
    source.add_argument("--files", nargs="+", help="lista ścieżek („-” czyta ze standardowego wejścia)")
    source.add_argument("--all", action="store_true", help="pełny plan bez patrzenia na zmiany")
    parser.add_argument("--fast", action="store_true",
                        help="podpowiedź bez tagów " + ", ".join(FAST_EXCLUDE_TAGS))
    parser.add_argument("--json", action="store_true", help="plan jako JSON")
    parser.add_argument("--github", action="store_true",
                        help="zapisz wyjścia do $GITHUB_OUTPUT (bez niego – na stdout)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # Windows z przekierowaniem do pliku: strzałki i cudzysłowy bez wyjątku
        sys.stdout.reconfigure(errors="replace")

    if args.all:
        plan = full_plan("Pełny plan na żądanie.", fast=args.fast)
    elif args.base is not None:
        changes = changes_from_git(args.base)
        if changes is None:
            plan = full_plan(f"Nie udało się ustalić zmian względem „{args.base}” – pełna suita.", fast=args.fast)
        else:
            paths, changed_lines = changes
            plan = build_plan(paths, lambda path: changed_lines.get(path, ""), fast=args.fast, base=args.base)
    else:
        plan = build_plan(read_files_argument(args.files), read_text, fast=args.fast,
                          sql_scope="całe pliki (bez gita)")

    if args.github:
        write_github(plan)
    if args.json:
        print(json.dumps(asdict(plan), ensure_ascii=False, indent=2))
    elif not args.github:
        print(describe(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
