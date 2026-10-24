"""Bounded library-based conversion in a disposable Python subprocess."""
import os
import time
import sys
import json
import logging
import re
import posixpath
import shutil
import signal
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory, SpooledTemporaryFile
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

from django.conf import settings
from lxml import etree

from .document_images import is_linked_image, linked_images_note

logger = logging.getLogger(__name__)

FORMATS = {'pdf': 'application/pdf', 'epub': 'application/epub+zip'}
MAX_OUTPUT = 50 * 1024 * 1024
# summary.json: liczniki reguł i lista zamian słownikowych (najwyżej 200 pozycji).
SUMMARY_LIMIT = 256 * 1024
# Rozpakowana część XML dokumentu (np. word/document.xml): większa zjada w podprocesie gigabajty pamięci.
DOCUMENT_XML_LIMIT = 15 * 1024 * 1024
TIME_LIMIT = 90
# Najdłuższy dopuszczalny limit (zadania superusera, patrz program_jobs.SUPERUSER_TIME_LIMIT).
MAX_TIME_LIMIT = 600
TIMEOUT_MESSAGE = "Konwersja przekroczyła limit {} sekund. Podziel dokument lub wybierz mniej formatów."


REBUILD_WARNING = 'W nowym DOCX zostaną pominięte: '


class ConversionError(Exception):
    pass



class RebuildConfirmationRequired(ConversionError):
    pass


BUSY_MESSAGE = 'Konwerter przetwarza teraz inny dokument. Spróbuj za chwilę.'


def _try_lock(lock):
    if os.name == 'posix':
        import fcntl
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    import msvcrt
    lock.seek(0)
    lock.write(b'0')
    lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


@contextmanager
def conversion_slot(wait=0, on_wait=None):
    """One conversion per shared project directory, across web workers.

    wait > 0: czekaj w kolejce najwyżej tyle sekund; on_wait() jest wołane
    co sekundę (np. sprawdzenie anulowania i znak życia zadania).
    """
    directory = Path(getattr(settings, 'DOCUMENT_CONVERSION_DIR', settings.BASE_DIR / 'var' / 'conversion'))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'conversion.lock').open('a+b') as lock:
        deadline = time.monotonic() + max(0, wait)
        while not _try_lock(lock):
            if time.monotonic() >= deadline:
                raise ConversionError(BUSY_MESSAGE if not wait else
                                      'Konwerter był zajęty innymi dokumentami zbyt długo. Spróbuj ponownie później.')
            if on_wait is not None:
                on_wait()
            time.sleep(1)
        try:
            yield directory
        finally:
            if os.name == "posix":
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


# Relacje obrazów i osadzonych plików: proces roboczy usuwa je przed pracą programów, więc brak pliku nie szkodzi.
OPTIONAL_TARGETS = ("/image", "/oleObject", "/package", "/hdphoto", "/media", "/video", "/audio",
                    "/metadata/thumbnail")
# Deklaracja DTD lub encji w części XML (Word jej nie zapisuje): także w UTF-16.
DOCTYPE_MARKERS = tuple(marker.encode(encoding) for marker in ("<!DOCTYPE", "<!ENTITY")
                        for encoding in ("ascii", "utf-16-le", "utf-16-be"))
DAMAGED_MESSAGE = ("Dokument jest uszkodzony – brakuje jednej z jego części. "
                   "Otwórz go w Wordzie lub LibreOffice i zapisz ponownie.")
# Zewnętrzne relacje inne niż hiperłącza, szablon i obrazy połączone (np. połączony obiekt OLE, film, dźwięk).
LINKED_FILE_MESSAGE = ("Dokument zawiera podłączony plik zewnętrzny (np. obiekt, film lub dźwięk). "
                       "Osadź go w DOCX przed konwersją.")


def validate_resources(source):
    """Do not let conversion fetch linked files, remote images or templates.

    Plik innego formatu (np. .doc, ODT, zaszyfrowany DOCX) albo nieobsługiwana odmiana DOCX dostaje własny
    komunikat; deklaracja DTD w dowolnej części XML i brak części, do której prowadzi relacja (python-docx
    przerwałby wtedy pracę), też kończą się czytelnym błędem.
    """
    from .document_errors import MESSAGES, foreign_format

    source.seek(0)
    kind = foreign_format(source.read())
    source.seek(0)
    if kind:
        raise ConversionError(MESSAGES[kind])
    try:
        with ZipFile(source) as archive:
            names = archive.namelist()
            if any(
                name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/")
                for name in names
            ):
                raise ConversionError("Nieprawidłowa struktura dokumentu DOCX.")
            limit = int(getattr(settings, "DOCUMENT_XML_LIMIT", DOCUMENT_XML_LIMIT))
            if any(item.filename.lower().endswith(".xml") and item.file_size > limit for item in archive.infolist()):
                raise ConversionError(MESSAGES["document_too_large"])
            if len(names) != len(set(names)):
                raise ConversionError(
                    "Dokument zawiera powielone elementy. Zapisz go ponownie w Wordzie."
                )
            present = set(names)
            for name in names:
                if name.lower().endswith(".xml"):
                    with archive.open(name) as part:
                        if any(marker in part.read(65536) for marker in DOCTYPE_MARKERS):
                            raise ConversionError("Nieobsługiwany dokument DOCX.")
                if name.lower().endswith(".rels"):
                    try:
                        root = etree.fromstring(
                            archive.read(name),
                            etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False),
                        )
                    except etree.XMLSyntaxError:
                        raise ConversionError("Nieprawidłowa struktura dokumentu DOCX.") from None
                    if root.getroottree().docinfo.doctype:
                        raise ConversionError("Nieobsługiwany dokument DOCX.")
                    for relationship in root:
                        target = relationship.get("Target", "")
                        if relationship.get("TargetMode", "").lower() != "external":
                            # Ścieżka zaczynająca się od „/” jest w OOXML liczona od korzenia pakietu.
                            if target.startswith("/") and not target.startswith("//"):
                                resolved = posixpath.normpath(target.lstrip("/"))
                            else:
                                base = posixpath.dirname(posixpath.dirname(name))
                                resolved = posixpath.normpath(posixpath.join(base, target))
                            if (
                                target.startswith(("//", "\\"))
                                or ":" in target
                                or "\\" in target
                                or resolved == ".."
                                or resolved.startswith("../")
                            ):
                                raise ConversionError(
                                    "Dokument zawiera nieprawidłowe odwołanie do pliku."
                                )
                            folder, rels_file = posixpath.split(name)
                            owner = posixpath.join(posixpath.dirname(folder), rels_file[: -len(".rels")])
                            if (target and resolved not in present
                                    and (owner == "" or owner in present)
                                    and not relationship.get("Type", "").endswith(OPTIONAL_TARGETS)):
                                raise ConversionError(DAMAGED_MESSAGE)
                        if relationship.get("TargetMode", "").lower() == "external":
                            # Szablon dołączony przez Worda (attachedTemplate) to tylko ścieżka
                            # w ustawieniach – żadna biblioteka go nie pobiera. Obraz połączony
                            # z plikiem zewnętrznym proces roboczy usuwa przed pracą programów
                            # (unlink_external_images, decyzja właściciela: usuwamy sam link).
                            if not (relationship.get("Type", "").endswith(("/hyperlink", "/attachedTemplate"))
                                    or is_linked_image(name, relationship)):
                                raise ConversionError(LINKED_FILE_MESSAGE)
    except (BadZipFile, OSError, EOFError, NotImplementedError, RuntimeError):
        # Nie ZIP, uszkodzony ZIP, nieobsługiwana kompresja lub szyfrowanie.
        raise ConversionError("Nie można odczytać pliku. Wybierz prawidłowy DOCX.") from None
    finally:
        source.seek(0)


def converter_python():
    """WSGI's sys.executable may be uwsgi, not a Python interpreter."""
    configured = getattr(settings, "DOCUMENT_CONVERTER_PYTHON", "") or os.environ.get(
        "DOCUMENT_CONVERTER_PYTHON", ""
    )

    def usable(path):
        return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))

    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute() or not usable(candidate):
            raise ConversionError(
                "DOCUMENT_CONVERTER_PYTHON musi wskazywać istniejący interpreter Pythona (pełna ścieżka)."
            )
        return str(candidate)
    import django

    roots = [
        parent
        for parent in Path(django.__file__).absolute().parents
        if (parent / "pyvenv.cfg").is_file()
    ]
    roots.append(Path(sys.prefix))
    for root in roots:
        candidate = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if usable(candidate):
            # Do not resolve symlinks: the venv path selects its site-packages.
            return str(candidate.absolute())
    candidate = Path(sys.executable)
    if re.fullmatch(
        r"python(?:[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?", candidate.name, re.IGNORECASE
    ) and usable(candidate):
        return str(candidate.absolute())
    raise ConversionError(
        "Nie znaleziono Pythona do konwersji. Ustaw DOCUMENT_CONVERTER_PYTHON na interpreter środowiska projektu."
    )


def run_converter(directory, timeout, control=None):
    if timeout <= 0:
        raise ConversionError(TIMEOUT_MESSAGE.format(TIME_LIMIT))
    # Do not pass project credentials to an external document processor.
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "PYTHONPATH")
    }
    env.update(
        HOME=str(directory),
        TMPDIR=str(directory),
        TEMP=str(directory),
        TMP=str(directory),
        PYTHONDONTWRITEBYTECODE="1",
    )
    command = [
        converter_python(),
        str(Path(__file__).with_name("document_worker_bootstrap.py")),
        str(directory),
    ]
    try:
        with subprocess.Popen(
            command,
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        ) as process:
            try:
                if control is None:
                    code = process.wait(timeout=timeout)
                else:
                    deadline = time.monotonic() + timeout
                    while True:
                        control(directory)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, timeout)
                        try:
                            code = process.wait(timeout=min(0.25, remaining))
                            control(directory)
                            break
                        except subprocess.TimeoutExpired:
                            if time.monotonic() >= deadline:
                                raise
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
                raise ConversionError(TIMEOUT_MESSAGE.format(TIME_LIMIT)) from None
            except BaseException:
                if process.poll() is None:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                process.wait()
                raise
    except OSError:
        raise ConversionError(
            "Nie można uruchomić konwertera. Administrator musi sprawdzić środowisko Pythona na serwerze."
        ) from None
    if code != 0:
        report = {}
        try:
            error_file = directory / "error.json"
            if error_file.stat().st_size <= 32768:
                report = json.loads(error_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not report:
            report = {
                "stage": "start",
                "error": "ProcessTerminated",
                "exit": code,
                "signal": -code if code < 0 else None,
            }
        if report.get("error") != "RebuildUnsupported":
            logger.error(
                "Błąd konwertera: exit=%s python=%s diagnostics=%s", code, command[0], report
            )
        stage = report.get("stage", "")
        stage = stage if stage in ("start", "DOCX", "PDF", "EPUB", "Powtórzenia") else "konwersja"
        kind = report.get("error", "")
        kind = kind if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", kind) else "błąd procesu"
        if kind == "RebuildUnsupported":
            raise RebuildConfirmationRequired(
                REBUILD_WARNING
                + "; ".join(str(item) for item in report.get("omissions", [])[:30])
                + ". Po akceptacji powstanie nowy DOCX bez tych elementów. Tekst i obsługiwane formatowanie zostaną przeniesione."
            )
        from .document_errors import MESSAGES

        if kind == "MemoryError":
            raise ConversionError(MESSAGES["memory_limit"])
        public_code = report.get("public_code")
        if (
            kind == "DocumentInputError"
            and isinstance(public_code, str)
            and public_code in MESSAGES
        ):
            raise ConversionError(MESSAGES[public_code])
        detail = f"{stage}: {kind}"
        if code == 2:
            raise ConversionError(
                "Brakuje bibliotek konwertera. Zainstaluj requirements.txt. "
                + detail
                + ". Szczegóły zapisano w logu błędów."
            )
        raise ConversionError(
            "Nie udało się przygotować plików ("
            + detail
            + "). Szczegóły zapisano w logu błędów. Nie oznacza to automatycznie uszkodzenia dokumentu; możesz pobrać oryginały po wyłączeniu konwersji."
        )


def conversion_filename(name, extension):
    # Preserve Unicode and spaces, but never put paths or controls in a ZIP entry.
    basename = str(name or "Dokument.docx").replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"[\x00-\x1f\x7f]", "", Path(basename).stem).strip() or "Dokument"
    if stem in (".", ".."):
        stem = "Dokument"
    return stem + "." + extension


def convert_document(
    upload,
    formats,
    *,
    use_cleaner=False,
    timeout=TIME_LIMIT,
    include_docx=False,
    rebuild=False,
    normalize=True,
    allow_rebuild_omissions=False,
    cleaner_rules=None,
    repetitions=None,
    justify=False,
    preserve_filename=False,
    remove_soft_whitespace=False,
    track_changes=False,
    cleaner_summary=False,
    replacements=None,
    keep_page_format=False,
    analysis_unlimited=False,
    control=None,
    wait_for_slot=0,
    on_wait=None,
):
    selected = [kind for kind in FORMATS if kind in formats]
    if (not selected and not include_docx) or set(formats) - set(FORMATS):
        raise ConversionError("Wybierz co najmniej jeden obsługiwany format.")
    validate_resources(upload)
    result = SpooledTemporaryFile(max_size=4 * 1024 * 1024, mode="w+b")
    try:
        with (
            conversion_slot(wait_for_slot, on_wait) as root,
            TemporaryDirectory(prefix="document-", dir=root) as temporary,
        ):
            # Limit czasu liczy się od uzyskania konwertera, nie od wejścia do kolejki.
            limit = min(MAX_TIME_LIMIT, timeout)
            deadline = time.monotonic() + limit
            directory = Path(temporary)
            source = _write_job(
                directory,
                upload,
                {
                    "formats": selected,
                    "prepare": True,
                    "clean": use_cleaner,
                    "cleaner_rules": cleaner_rules,
                    "rebuild": rebuild,
                    "allow_rebuild_omissions": allow_rebuild_omissions,
                    "remove_soft_whitespace": remove_soft_whitespace,
                    "normalize": normalize,
                    "justify": justify,
                    "include_docx": include_docx,
                    "repetitions": repetitions,
                    "track_changes": bool(track_changes),
                    "cleaner_summary": bool(cleaner_summary),
                    "replacements": replacements,
                    "keep_page_format": bool(keep_page_format),
                    "analysis_unlimited": bool(analysis_unlimited),
                },
            )
            try:
                if control is None:
                    run_converter(directory, deadline - time.monotonic())
                else:
                    control(directory)
                    run_converter(directory, deadline - time.monotonic(), control=control)
            except ConversionError as error:
                # run_converter zna tylko pozostały czas; komunikat podaje faktyczny limit zadania.
                if limit > TIME_LIMIT and str(error) == TIMEOUT_MESSAGE.format(TIME_LIMIT):
                    raise ConversionError(TIMEOUT_MESSAGE.format(int(limit))) from None
                raise
            warning_file = directory / "warnings.json"
            warnings = (
                json.loads(warning_file.read_text(encoding="utf-8"))
                if warning_file.is_file()
                else []
            )
            result.conversion_warnings = warnings
            summary_file = directory / "summary.json"
            result.cleaner_summary = None
            if cleaner_summary and summary_file.is_file() and summary_file.stat().st_size <= SUMMARY_LIMIT:
                summary = json.loads(summary_file.read_text(encoding="utf-8"))
                if isinstance(summary, dict):
                    result.cleaner_summary = {
                        str(key)[:80]: value for key, value in summary.items()
                        if isinstance(value, int) and not isinstance(value, bool) and value > 0
                    }
                    replacements = _replacement_list(summary.get("replacements"))
                    if replacements:
                        result.cleaner_summary["replacements"] = replacements
            result.removed_images = _removed_images(directory / "images.json")
            # Obrazy tylko połączone z plikami zewnętrznymi (usunięte razem z linkiem) – osobna uwaga.
            note = linked_images_note(_removed_images(directory / "images.json", "linked_images"))
            if note:
                result.conversion_warnings = [note, *warnings][:30]
            outputs = [directory / ("document." + kind) for kind in selected]
            pdf_error = directory / "pdf_error.json"
            if (
                pdf_error.is_file()
                and not (directory / "document.pdf").exists()
                and (directory / "document.epub").is_file()
            ):
                # Nieudany PDF nie zabiera gotowego EPUB; uwaga o tym jest już w warnings.json.
                try:
                    logger.error("Błąd PDF (EPUB wydany): %s", pdf_error.read_text(encoding="utf-8")[:4000])
                except OSError:
                    pass
                outputs = [target for target in outputs if target.suffix != ".pdf"]
            if include_docx:
                shutil.copyfile(source, directory / "document.docx")
                outputs.append(directory / "document.docx")
            for target in outputs:
                if (
                    not target.is_file()
                    or target.is_symlink()
                    or not 0 < target.stat().st_size <= MAX_OUTPUT
                ):
                    raise ConversionError(
                        "Konwersja nie utworzyła poprawnego pliku wynikowego lub wynik przekroczył 50 MB."
                    )
            # Uwagi konwertera trafiają do conversion_warnings (strona postępu
            # pokazuje je użytkownikowi); jeden plik wynikowy nie jest pakowany.
            if len(outputs) == 1:
                with outputs[0].open("rb") as converted:
                    shutil.copyfileobj(converted, result)
                extension = outputs[0].suffix.lstrip(".")
                mime = FORMATS.get(
                    extension,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            else:
                with ZipFile(result, "w", compression=ZIP_DEFLATED) as archive:
                    for output in outputs:
                        archive.write(
                            output,
                            arcname=conversion_filename(
                                getattr(upload, "name", "Dokument.docx"), output.suffix.lstrip(".")
                            )
                            if preserve_filename
                            else output.name,
                        )
                extension, mime = "zip", "application/zip"
            finished = time.monotonic()
        if finished > deadline:
            raise ConversionError(
                "Przygotowanie dokumentu przekroczyło limit czasu. Wybierz mniej formatów lub krótszy dokument."
            )
        result.seek(0)
        return result, extension, mime
    except BaseException:
        result.close()
        raise


def _replacement_list(value):
    """Lista zamian słownikowych z summary.json: [{source, target, count}], tylko poprawne pozycje."""
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:200]:
        if (isinstance(item, dict) and isinstance(item.get("source"), str) and isinstance(item.get("target"), str)
                and isinstance(item.get("count"), int) and not isinstance(item.get("count"), bool)
                and item["count"] > 0):
            result.append({"source": item["source"][:200], "target": item["target"][:200], "count": item["count"]})
    return result


def _removed_images(path, key="removed_images"):
    """Liczba obrazów usuniętych przez podproces (images.json; key="linked_images" – obrazów tylko
    połączonych z plikami zewnętrznymi); 0, gdy pliku nie ma lub jest błędny."""
    try:
        if path.is_file() and path.stat().st_size <= 1024:
            value = json.loads(path.read_text(encoding="utf-8")).get(key, 0)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    except (OSError, ValueError, AttributeError):
        pass
    return 0


def _write_job(directory, upload, config):
    source = directory / "source.docx"
    upload.seek(0)
    with source.open("wb") as destination:
        shutil.copyfileobj(upload, destination)
    config["title"] = Path(getattr(upload, "name", "Dokument.docx")).stem[:200] or "Dokument"
    (directory / "job.json").write_text(json.dumps(config), encoding="utf-8")
    return source


def inspect_document(upload, *, timeout=TIME_LIMIT, wait_for_slot=0):
    """Bounded rebuild preflight; no converted document is constructed."""
    validate_resources(upload)
    with conversion_slot(wait_for_slot) as root, TemporaryDirectory(prefix="document-", dir=root) as temporary:
        deadline = time.monotonic() + min(TIME_LIMIT, timeout)
        directory = Path(temporary)
        _write_job(directory, upload, {"formats": [], "inspect": True})
        run_converter(directory, deadline - time.monotonic())
