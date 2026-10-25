"""Testy skryptu wyboru testów według zmienionych plików (bez Django): ``python -m unittest tools.ci.test_affected``."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

try:
    from tools.ci import affected
except ImportError:  # uruchomione z katalogu tools/ci
    import affected

def plan_for(*paths, sql=None, **kwargs):
    """Plan dla ścieżek; ``sql`` to słownik ścieżka → tekst albo funkcja (domyślnie heurystyka nic nie znajduje)."""
    lookup = sql if callable(sql) else (sql or {}).get
    return affected.build_plan(list(paths), lambda path: lookup(path) or "", **kwargs)


class GlobTests(unittest.TestCase):
    def test_patterns(self):
        """`*` nie przechodzi przez `/`, `**` przechodzi, a wzorzec musi objąć całą ścieżkę."""
        cases = [
            ("*.md", "README.md", True),
            ("*.md", "docs/plan.md", False),
            ("**/*.md", "docs/plan.md", True),
            ("**/*.md", "README.md", True),
            ("core/tests/*/**", "core/tests/site/test_site_www.py", True),
            ("core/tests/*/**", "core/tests/test_runtime_command.py", False),
            ("*/models.py", "people/models.py", True),
            ("*/models.py", "core/site_models.py", False),
            ("*/*_models.py", "core/site_models.py", True),
            ("*/migrations/**", "core/migrations/0001_initial.py", True),
            ("core/static/core/program-*.js", "core/static/core/program-jobs.js", True),
            ("core/static/core/program-*.js", "core/static/core/programs.css", False),
            ("**/tests/**", "workflow/tests/support.py", True),
            ("**/tests/**", "workflow/services.py", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(affected.matches(path, (pattern,)), expected)

    def test_normalize(self):
        """Ścieżki z Windows i z `./` są sprowadzane do postaci z `/`."""
        self.assertEqual(affected.normalize(r".\core\static\core\pages.css"), "core/static/core/pages.css")
        self.assertEqual(affected.normalize(" README.md\n"), "README.md")


class SingleFileTests(unittest.TestCase):
    def test_rule_for_file(self):
        """Każdy obszar: oczekiwany tryb SQLite, etykiety i MySQL dla pojedynczego zmienionego pliku."""
        cases = [
            # (ścieżka, sqlite, etykiety, mysql)
            ("README.md", "none", [], False),
            ("import_data/autorzy.tsv", "none", [], False),
            (".env.example", "none", [], False),
            ("tools/ci/affected.py", "none", [], False),
            ("core/static/core/pages.css", "affected", ["core.tests.ui"], False),
            (".stylelintrc.json", "affected", ["core.tests.ui"], False),
            ("core/static/core/dialogs.js", "affected", ["core.tests.ui"], False),
            ("core/js_tests/support/dom.js", "affected", ["core.tests.ui"], False),
            ("core/static/core/fantazmaty-logo.png", "affected", ["core.tests.ui"], False),
            ("core/static/core/program-jobs.js", "affected",
             ["core.tests.programs", "core.tests.recruitment", "core.tests.ui"], False),
            ("core/static/core/odkurzacz.js", "affected",
             ["core.tests.programs", "core.tests.recruitment", "core.tests.ui"], False),
            ("tools/wordpress/fz-ekipa.php", "affected", ["core.tests.site"], False),
            ("core/services/odkurzacz.py", "affected", ["core.tests.programs", "core.tests.recruitment"], False),
            ("core/services/document_rebuild.py", "affected",
             ["core.tests.programs", "core.tests.recruitment"], False),
            ("core/services/program_jobs.py", "affected", ["core.tests.programs", "core.tests.recruitment"], False),
            ("core/views/replacements.py", "affected", ["core.tests.programs", "core.tests.recruitment"], False),
            ("core/program_models.py", "affected", ["core.tests.programs", "core.tests.recruitment"], False),
            ("core/templates/core/programs.html", "affected",
             ["core.tests.programs", "core.tests.recruitment"], False),
            ("core/templates/core/includes/program_rules.html", "affected",
             ["core.tests.programs", "core.tests.recruitment"], False),
            ("tools/redaktor_docx/build.py", "affected", ["core.tests.programs", "core.tests.recruitment"], False),
            ("core/views/site_www.py", "affected", ["core.tests.site"], False),
            ("core/site_catalog.py", "affected", ["core.tests.site"], False),
            ("core/site_listen_seed.json", "affected", ["core.tests.site"], False),
            ("core/templates/core/site_catalog.html", "affected", ["core.tests.site"], False),
            ("core/views/social_media.py", "affected", ["core.tests.social"], False),
            ("core/social_forms.py", "affected", ["core.tests.social"], False),
            ("core/tests/site/test_site_www.py", "affected", ["core.tests.site"], False),
            ("core/tests/programs/support.py", "affected", ["core.tests.programs"], False),
            ("core/tests/test_runtime_command.py", "affected", ["core.tests.test_runtime_command"], False),
            ("workflow/tests/test_reports.py", "affected", ["workflow"], False),
            ("illustrations/tests/test_cover_proposals.py", "affected", ["illustrations"], False),
            ("illustrations/views.py", "affected", ["core.tests.ui", "illustrations"], False),
            ("illustrations/forms.py", "affected", ["core.tests.ui", "illustrations"], False),
            # Pełna suita SQLite bez MySQL.
            ("core/tests/__init__.py", "full", [], False),
            ("fantazmaty/testing/data.py", "full", [], False),
            ("core/views/texts.py", "full", [], False),
            ("core/templates/core/base.html", "full", [], False),
            ("core/templates/admin/base_site.html", "full", [], False),
            ("core/urls.py", "full", [], False),
            ("core/audiobook_forms.py", "full", [], False),
            (".github/workflows/tests.yml", "full", [], False),
            ("pyproject.toml", "full", [], False),
            # Pełna suita SQLite i MySQL.
            ("people/models.py", "full", [], True),
            ("core/audiobook_models.py", "full", [], True),
            ("illustrations/models.py", "full", [], True),
            ("core/migrations/0071_recruitment_remove_department.py", "full", [], True),
            ("illustrations/migrations/0001_initial.py", "full", [], True),
            ("core/selectors/texts.py", "full", [], True),
            ("workflow/services.py", "full", [], True),
            ("workflow/read_queries.py", "full", [], True),
            ("core/table_sorting.py", "full", [], True),
            ("core/sort_keys.py", "full", [], True),
            ("core/search_lookup.py", "full", [], True),
            ("core/middleware.py", "full", [], True),
            ("core/activity.py", "full", [], True),
            ("core/activity_spool.py", "full", [], True),
            ("core/supervision.py", "full", [], True),
            ("core/services/texts.py", "full", [], True),
            ("core/services/vacations.py", "full", [], True),
            ("core/services/reviews.py", "full", [], True),
            ("fantazmaty/settings.py", "full", [], True),
            ("fantazmaty/test_settings.py", "full", [], True),
            ("fantazmaty/mysql_integration_settings.py", "full", [], True),
            ("requirements.txt", "full", [], True),
            ("requirements-dev.txt", "full", [], True),
        ]
        for path, sqlite, labels, mysql in cases:
            with self.subTest(path=path):
                plan = plan_for(path)
                self.assertEqual((plan.sqlite, plan.labels, plan.mysql), (sqlite, labels, mysql))

    def test_flags_for_file(self):
        """Flagi stylelint, JS, PHP, ruff, pip-audit i kontroli wydania zależą od rodzaju pliku."""
        cases = [
            # (ścieżka, css, js, php, ruff, dependencies, release)
            ("core/static/core/pages.css", True, False, False, False, False, True),
            (".stylelintrc.json", True, False, False, False, False, False),
            ("core/static/core/dialogs.js", False, True, False, False, False, True),
            ("core/js_tests/dialogs.cjs", False, True, False, False, False, False),
            ("tools/wordpress/fz-katalog.php", False, False, True, False, False, False),
            ("tools/wordpress/sluchaj-css-dopisek.css", False, False, True, False, False, False),
            ("core/templates/core/home.html", False, False, False, False, False, True),
            ("core/views/texts.py", False, False, False, True, False, True),  # pełna suita → kontrola wydania
            ("core/services/odkurzacz.py", False, False, False, True, False, False),
            ("tools/ci/affected.py", False, False, False, True, False, False),
            ("pyproject.toml", False, False, False, True, False, True),
            ("requirements.txt", False, False, False, False, True, True),
            ("README.md", False, False, False, False, False, False),
        ]
        for path, css, js, php, ruff, dependencies, release in cases:
            with self.subTest(path=path):
                plan = plan_for(path)
                self.assertEqual((plan.css, plan.js, plan.php, plan.ruff, plan.dependencies, plan.release),
                                 (css, js, php, ruff, dependencies, release))


class SqlHeuristicTests(unittest.TestCase):
    def test_markers_in_changed_lines_force_mysql(self):
        """Plik obszaru ze zmienioną linią kształtującą SQL przełącza plan na pełną suitę i MySQL."""
        markers = ["annotate(", "aggregate(", "Subquery", "OuterRef", "Exists(", "Case(", "When(", "Cast(",
                   "Coalesce(", "RawSQL", ".extra(", "Window(", "select_for_update", "connection.cursor",
                   "created__date", "created__week", "created__iso_year", "TruncDate", "Collate"]
        for marker in markers:
            with self.subTest(marker=marker):
                plan = plan_for("core/views/site_www.py", sql={"core/views/site_www.py": f"x = {marker}"})
                self.assertEqual((plan.sqlite, plan.mysql, plan.labels), ("full", True, []))
                self.assertIn("core/views/site_www.py", plan.mysql_reasons[0])
                self.assertEqual(plan.files[0]["sql"], affected.SQL_MARKERS.search(f"x = {marker}").group())

    def test_lines_without_markers_keep_area(self):
        """Zwykła zmiana w pliku obszaru (np. tekst komunikatu) zostaje w pakiecie obszaru."""
        plan = plan_for("core/views/site_www.py", sql={"core/views/site_www.py": "messages.success(request, 'OK')"})
        self.assertEqual((plan.sqlite, plan.mysql, plan.labels), ("affected", False, ["core.tests.site"]))

    def test_exempt_files_are_not_inspected(self):
        """Testów, pomocników testów, migracji, narzędzi i plików spoza .py heurystyka nie ogląda."""
        exempt = ["workflow/tests/test_reports.py", "core/tests.py", "core/test_dawny.py",
                  "fantazmaty/testing/history.py", "tools/redaktor_docx/build.py", "core/templates/core/home.html"]
        for path in exempt:
            with self.subTest(path=path):
                plan = plan_for(path, sql=lambda _path: "qs.annotate(")
                self.assertFalse(plan.mysql)
        plan = plan_for("core/migrations/0099_x.py", sql=lambda _path: "RawSQL")
        self.assertEqual(plan.mysql_reasons, ["core/migrations/0099_x.py: modele, migracje i selektory"])


class MixedChangeTests(unittest.TestCase):
    def test_labels_are_merged_and_sorted(self):
        """Kilka obszarów bez pełnej suity daje sumę ich pakietów (bez powtórzeń, posortowaną)."""
        plan = plan_for("core/static/core/pages.css", "core/services/odkurzacz.py", "README.md",
                        "core/tests/site/test_site_seed.py", "core/static/core/program-help.js")
        self.assertEqual(plan.sqlite, "affected")
        self.assertEqual(plan.labels, ["core.tests.programs", "core.tests.recruitment", "core.tests.site",
                                       "core.tests.ui"])
        self.assertEqual((plan.css, plan.js, plan.release, plan.mysql), (True, True, True, False))

    def test_one_unknown_file_means_full_suite(self):
        """Jeden nierozpoznany plik przełącza na pełną suitę SQLite, ale bez MySQL."""
        plan = plan_for("core/static/core/pages.css", "core/views/texts.py")
        self.assertEqual((plan.sqlite, plan.labels, plan.mysql, plan.css), ("full", [], False, True))
        self.assertEqual(plan.files[1]["rule"], affected.UNKNOWN)

    def test_only_ignored_files(self):
        plan = plan_for("README.md", "docs/plan.md", "LICENSE")
        self.assertEqual((plan.sqlite, plan.commands), ("none", []))

    def test_fast_adds_exclude_tags_to_command(self):
        """`--fast` dopisuje pomijane tagi do podpowiadanego polecenia, a bez niego polecenie ich nie ma."""
        plan = plan_for("core/services/odkurzacz.py", fast=True)
        self.assertEqual(plan.exclude_tags, ["slow", "migration", "docx"])
        self.assertIn("python manage.py test --settings=fantazmaty.test_settings core.tests.programs "
                      "core.tests.recruitment --parallel --exclude-tag slow --exclude-tag migration --exclude-tag docx",
                      plan.commands)
        self.assertNotIn("--exclude-tag", " ".join(plan_for("core/services/odkurzacz.py").commands))

    def test_commands_for_full_plan(self):
        plan = plan_for("people/models.py", "core/static/core/pages.css", "tools/wordpress/fz-ekipa.php")
        self.assertEqual(plan.commands, [
            "ruff check .",
            affected.STYLELINT,
            affected.PHP_TEST,
            "python manage.py test --settings=fantazmaty.test_settings --parallel",
            "python manage.py collectstatic --noinput --settings=fantazmaty.release_test_settings && python manage.py"
            " test --settings=fantazmaty.release_test_settings core.tests.ui core.tests.texts core.tests.security",
            "python manage.py test --settings=fantazmaty.mysql_integration_settings --noinput"
            "   # wymaga TEST_MYSQL_USER i TEST_MYSQL_PASSWORD",
        ])

    def test_everything(self):
        plan = affected.full_plan("wszystko")
        self.assertEqual((plan.sqlite, plan.labels, plan.mysql, plan.css, plan.js, plan.php, plan.dependencies,
                          plan.release), ("full", [], True, True, True, True, True, True))


class GitParsingTests(unittest.TestCase):
    def test_changed_lines_by_file(self):
        """Z wyniku git diff zbierane są tylko dodane i usunięte linie, osobno dla każdego pliku."""
        diff = (
            "diff --git a/core/views/texts.py b/core/views/texts.py\n"
            "index 1..2 100644\n--- a/core/views/texts.py\n+++ b/core/views/texts.py\n@@ -1,2 +1,2 @@\n"
            "-old = 1\n+new = qs.annotate(n=Count('x'))\n"
            "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n"
            "-# A\n+# B\n"
        )
        self.assertEqual(affected.changed_lines_by_file(diff), {
            "core/views/texts.py": "old = 1\nnew = qs.annotate(n=Count('x'))",
            "README.md": "# A\n# B",
        })

    def test_unusable_base_gives_none(self):
        """Zerowy SHA (nowa gałąź) i pusty ref nie są liczone – wołający bierze pełną suitę."""
        for base in ["", "0000000000000000000000000000000000000000"]:
            with self.subTest(base=base):
                self.assertIsNone(affected.changes_from_git(base))
        with mock.patch.object(affected, "run_git", return_value=None):
            self.assertIsNone(affected.changes_from_git("origin/main"))

    def test_changes_from_git_include_untracked_files(self):
        """Niezatwierdzone i niedodane pliki wchodzą do planu; nowy plik jest oglądany w całości."""
        answers = {
            ("merge-base", "origin/main", "HEAD"): "abc\n",
            ("diff", "--name-only", "abc"): "core/views/site_www.py\n",
            ("ls-files", "--others", "--exclude-standard"): "core/nowy.py\n",
            ("diff", "-U0", "--no-color", "abc"): "diff --git a/core/views/site_www.py b/core/views/site_www.py\n"
                                                  "+x = 1\n",
        }
        with mock.patch.object(affected, "run_git", side_effect=lambda *args: answers[args]), \
                mock.patch.object(affected, "read_text", return_value="qs.aggregate("):
            paths, changed = affected.changes_from_git("origin/main")
        self.assertEqual(paths, ["core/views/site_www.py", "core/nowy.py"])
        self.assertEqual(changed, {"core/views/site_www.py": "x = 1", "core/nowy.py": "qs.aggregate("})


class CommandLineTests(unittest.TestCase):
    def test_github_outputs_written_to_file(self):
        """`--github` dopisuje wyjścia do $GITHUB_OUTPUT i podsumowanie do $GITHUB_STEP_SUMMARY."""
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out.txt"
            summary = Path(folder) / "summary.md"
            env = {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
            with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as stdout:
                code = affected.main(["--files", "core/static/core/pages.css", "README.md", "--github"])
            self.assertEqual((code, stdout.getvalue()), (0, ""))
            self.assertEqual(output.read_text(encoding="utf-8").splitlines(), [
                "labels=core.tests.ui", "sqlite=affected", "mysql=false", "release=true", "css=true", "js=false",
                "php=false", "dependencies=false", "exclude_tags=",
            ])
            self.assertIn("core.tests.ui", summary.read_text(encoding="utf-8"))

    def test_github_outputs_fall_back_to_stdout(self):
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": ""}), \
                redirect_stdout(io.StringIO()) as stdout:
            affected.main(["--all", "--github"])
        self.assertIn("sqlite=full\nmysql=true\n", stdout.getvalue())

    def test_json_output(self):
        with redirect_stdout(io.StringIO()) as stdout:
            affected.main(["--files", "core/services/odkurzacz.py", "--json", "--fast"])
        plan = json.loads(stdout.getvalue())
        self.assertEqual((plan["sqlite"], plan["labels"], plan["exclude_tags"]),
                         ("affected", ["core.tests.programs", "core.tests.recruitment"], ["slow", "migration", "docx"]))

    def test_long_lists_are_shortened_in_text(self):
        """Opis tekstowy pokazuje najwyżej 40 plików i 10 powodów MySQL; reszta jest liczona."""
        paths = [f"core/migrations/{n:04d}_x.py" for n in range(50)]
        text = affected.describe(plan_for(*paths))
        self.assertIn("… i 10 więcej (pełna lista: --json)", text)
        self.assertIn("… i 40 więcej (pełna lista: --json)", text)
        self.assertEqual(affected.shortened(["a", "b"], 2), ["a", "b"])

    def test_files_from_stdin(self):
        with mock.patch.object(affected.sys, "stdin", io.StringIO("README.md\ncore/static/core/pages.css\n")), \
                redirect_stdout(io.StringIO()) as stdout:
            affected.main(["--files", "-"])
        self.assertIn("SQLite:  core.tests.ui", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
