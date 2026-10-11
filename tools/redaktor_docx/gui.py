# ruff: noqa: F821
# ===== Program lokalny: interfejs (ta część jest doklejana przez tools/redaktor_docx/build.py) =====
# Nazwy bez definicji w tym pliku (EDITORIAL_RULES, prepare_docx, color_document, convert, …)
# pochodzą ze scalonych wyżej modułów core/services.
import base64
import json
from io import BytesIO  # noqa: F401 – używane też przez scalone moduły
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import zlib
from pathlib import Path

PROGRESS_CALLBACK = None


def report(stage, completed=None, total=None):  # zastępuje wersję zapisującą postęp do pliku
    callback = PROGRESS_CALLBACK
    if callback is None:
        return
    if total and completed is not None:
        stage = f'{stage} ({completed}/{total})'
    callback(stage)


report_progress = report


def _bundled_font_dir():
    """Czcionki PDF wypakowane z tego pliku do katalogu ustawień (raz, przy pierwszym użyciu)."""
    directory = _settings_dir() / 'fonts'
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in BUNDLED_FONTS.items():
        target = directory / name
        data = zlib.decompress(base64.b64decode(payload))
        if not target.exists() or target.stat().st_size != len(data):
            target.write_bytes(data)
    return directory


def _settings_dir():
    base = os.environ.get('APPDATA') if os.name == 'nt' else os.environ.get('XDG_CONFIG_HOME')
    return (Path(base) if base else Path.home() / '.config') / 'RedaktorDOCX'


SUMMARY_LABELS = {**dict(EDITORIAL_RULES),
                  'nonbreaking_spaces': 'Spacje nierozdzielające i nietypowe zamienione na zwykłe'}
PROGRAMS = ('Odkurzacz', 'Konwersja', 'Powtórzenia', 'Przebudowa')


def default_settings():
    return {
        'version': 2, 'last_directory': '', 'open_result': True, 'strip_images': True,
        'rules': {key: key in FORM_DEFAULT_EDITORIAL_RULES for key, _ in EDITORIAL_RULES},
        'remove_soft_whitespace': True, 'normalize_formatting': False, 'justify': False,
        'track_changes': False, 'replacements': replacements_text(DEFAULT_REPLACEMENTS),
        'formats': {'pdf': True, 'epub': True}, 'convert_clean': True, 'convert_rebuild': False,
        'window_size': WINDOW_SIZE, 'min_word_length': MIN_WORD_LENGTH, 'include_prefix_matches': False,
        'color_palette': 'dark', 'ignored_words': '', 'tracked_words': '', **CHECK_DEFAULTS,
        'allow_omissions': False, 'keep_page_format': False,
    }


def replacements_text(entries):
    """Słownik zamian jako tekst: „źródło = cel”, wzorce z przedrostkiem „re:”."""
    lines = []
    for entry in entries:
        prefix = 're:' if entry['kind'] == 'pattern' else ''
        lines.append(f"{prefix}{entry['source']} = {entry['target']}")
    return '\n'.join(lines)


def parse_replacements(text):
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or ' = ' not in line:
            continue
        source, target = line.split(' = ', 1)
        kind = 'word'
        if source.startswith('re:'):
            kind, source = 'pattern', source[3:]
        if source.strip():
            entries.append({'kind': kind, 'source': source.strip(), 'target': target})
    return entries


def load_settings():
    defaults = default_settings()
    path = _settings_dir() / 'ustawienia.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return defaults, ''
    except (OSError, ValueError) as exc:
        return defaults, f'Nie udało się odczytać ustawień: {exc}'
    if not isinstance(raw, dict):
        return defaults, 'Nieprawidłowy format ustawień – użyto domyślnych.'
    for key, value in defaults.items():
        candidate = raw.get(key)
        if isinstance(value, dict):
            if isinstance(candidate, dict):
                value.update({k: bool(v) for k, v in candidate.items() if k in value and isinstance(v, bool)})
        elif isinstance(value, bool):
            if isinstance(candidate, bool):
                defaults[key] = candidate
        elif isinstance(value, int):
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
                defaults[key] = candidate
        elif isinstance(candidate, str):
            defaults[key] = candidate
    return defaults, ''


def save_settings(settings):
    path = _settings_dir() / 'ustawienia.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False)
    with handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)
    os.replace(handle.name, path)


def open_result_file(path):
    absolute = str(Path(path).resolve())
    if os.name == 'nt':
        os.startfile(absolute)  # noqa: S606 – otwarcie wyniku w domyślnym programie
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', absolute],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _prepare_source(path, strip):
    """Tak jak w CMS: najpierw usunięcie obrazów i narzucenie języka polskiego (opcjonalne lokalnie)."""
    with open(path, 'rb') as handle:
        data = BytesIO(handle.read())
    if not strip:
        return data, 0
    stripped, removed = strip_images(data)
    return force_polish(stripped), removed


def run_program(program, source, settings, progress):
    """Uruchomienie jednego programu; zwraca listę komunikatów do pokazania."""
    global PROGRESS_CALLBACK
    PROGRESS_CALLBACK = progress
    source = Path(source)
    notes = []
    rules = [key for key, enabled in settings['rules'].items() if enabled]
    try:
        progress('Przygotowanie dokumentu…')
        payload, removed = _prepare_source(source, settings['strip_images'])
        if removed:
            notes.append(f'Usunięto obrazy: {removed}.')
        if program == 'Przebudowa':
            progress('Przebudowa DOCX…')
            try:
                inspect_docx(BytesIO(payload.getvalue()), keep_page_format=settings['keep_page_format'])
            except RebuildUnsupported as error:
                if not settings['allow_omissions']:
                    raise ValueError('Przebudowa pominęłaby elementy: ' + '; '.join(error.omissions)
                                     + '\n\nZaznacz „Pozwól pominąć nieobsługiwane elementy”, aby mimo to przebudować.')
                notes.append('Pominięte elementy: ' + '; '.join(error.omissions))
            result = rebuild_docx(payload, allow_omissions=True, keep_page_format=settings['keep_page_format'])
            target = source.with_name(source.stem + '_przebudowa.docx')
            target.write_bytes(result.getvalue())
            return target, notes
        if program == 'Odkurzacz':
            summary = {}
            result = prepare_docx(
                payload, normalize_formatting=settings['normalize_formatting'], justify=settings['justify'],
                remove_soft_whitespace=settings['remove_soft_whitespace'], cleaner_rules=rules, use_cleaner=True,
                track_changes=settings['track_changes'], replacements=parse_replacements(settings['replacements']),
                summary=summary)
            target = source.with_name(source.stem + '_odkurzony.docx')
            target.write_bytes(result.getvalue())
            pairs = summary.pop('replacements', None)
            rows = [f"{SUMMARY_LABELS.get(key, key)}: {count}" for key, count in summary.items()]
            if pairs:
                rows.append('Zamiany słownikowe: ' + ', '.join(
                    f"{row['source']} → {row['target']} ({row['count']})" for row in pairs[:30]))
            notes.append('Podsumowanie zmian: ' + ('; '.join(rows) if rows else 'bez zmian.'))
            return target, notes
        if program == 'Konwersja':
            formats = [name for name, enabled in settings['formats'].items() if enabled]
            if not formats:
                raise ValueError('Wybierz PDF lub EPUB.')
            prepared = prepare_docx(
                payload, rebuild=settings['convert_rebuild'], allow_omissions=True,
                normalize_formatting=True, justify=settings['justify'],
                remove_soft_whitespace=settings['remove_soft_whitespace'],
                cleaner_rules=rules if settings['convert_clean'] else (), use_cleaner=settings['convert_clean'],
                replacements=parse_replacements(settings['replacements']))
            with tempfile.TemporaryDirectory(prefix='redaktor-docx-') as folder:
                directory = Path(folder)
                work = directory / 'source.docx'
                work.write_bytes(prepared.getvalue())
                convert(work, directory, formats, source.stem)
                targets = []
                for extension in formats:
                    produced = directory / f'document.{extension}'
                    if produced.exists():
                        target = source.with_suffix(f'.{extension}')
                        shutil.copyfile(produced, target)
                        targets.append(target)
                warnings = directory / 'warnings.json'
                if warnings.exists():
                    notes.extend(json.loads(warnings.read_text(encoding='utf-8')))
            if not targets:
                raise ValueError('Konwersja nie utworzyła żadnego pliku.')
            return targets[0] if len(targets) == 1 else targets, notes
        if program == 'Powtórzenia':
            prepared = prepare_docx(payload, remove_soft_whitespace=settings['remove_soft_whitespace'])
            progress('Analiza powtórzeń…')
            options = {key: settings[key] for key in CHECK_DEFAULTS}
            result = color_document(
                prepared, window_size=settings['window_size'], min_word_length=settings['min_word_length'],
                ignored_words=settings['ignored_words'], tracked_words=settings['tracked_words'],
                analysis_options=options, include_prefix_matches=settings['include_prefix_matches'],
                color_palette=settings['color_palette'], analysis_unlimited=True)
            target = source.with_name(source.stem + '_powtorzenia.docx')
            target.write_bytes(result.getvalue())
            return target, notes
        raise ValueError(f'Nieznany program: {program}')
    finally:
        PROGRESS_CALLBACK = None


def launch_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    settings, warning = load_settings()
    root = tk.Tk()
    root.title('Redaktor DOCX – Odkurzacz, konwersja, powtórzenia, przebudowa')
    root.minsize(900, 640)
    root.geometry('980x760')
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

    top = ttk.Frame(root, padding=(16, 14, 16, 6))
    top.grid(row=0, column=0, sticky='ew')
    top.columnconfigure(1, weight=1)
    path_var = tk.StringVar()
    ttk.Label(top, text='Plik DOCX:').grid(row=0, column=0, padx=(0, 10))
    path_entry = ttk.Entry(top, textvariable=path_var)
    path_entry.grid(row=0, column=1, sticky='ew')

    def choose_file():
        selected = filedialog.askopenfilename(parent=root, title='Wybierz dokument DOCX',
                                              initialdir=settings.get('last_directory') or None,
                                              filetypes=[('Dokument Word', '*.docx')])
        if selected:
            path_var.set(selected)

    browse = ttk.Button(top, text='Wybierz…', command=choose_file)
    browse.grid(row=0, column=2, padx=(8, 0))
    strip_var = tk.BooleanVar(value=settings['strip_images'])
    open_var = tk.BooleanVar(value=settings['open_result'])
    ttk.Checkbutton(top, text='Usuń obrazy i narzuć język polski (jak w CMS)', variable=strip_var).grid(
        row=1, column=0, columnspan=2, sticky='w', pady=(8, 0))
    ttk.Checkbutton(top, text='Otwórz wynik po zakończeniu', variable=open_var).grid(
        row=1, column=2, sticky='w', pady=(8, 0))

    notebook = ttk.Notebook(root)
    notebook.grid(row=1, column=0, sticky='nsew', padx=16)
    variables = {}

    def scrollable(parent):
        canvas = tk.Canvas(parent, highlightthickness=0)
        bar = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        bar.grid(row=0, column=1, sticky='ns')
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        inner = ttk.Frame(canvas, padding=12)
        window = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))

        def wheel(event):
            canvas.yview_scroll(-1 if getattr(event, 'delta', 0) > 0 or event.num == 4 else 1, 'units')
            return 'break'
        for name in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            canvas.bind_all(name, wheel, add='+')
        return inner

    # --- Odkurzacz ---
    tab = ttk.Frame(notebook)
    notebook.add(tab, text='Odkurzacz')
    inner = scrollable(tab)
    inner.columnconfigure(0, weight=1)
    ttk.Label(inner, wraplength=880, justify='left', text=(
        'Odkurzacz poprawia spacje, interpunkcję, cudzysłowy, myślniki i zapis liczb we wszystkich częściach '
        'dokumentu. Wynik zapisuje obok pliku źródłowego jako „nazwa_odkurzony.docx”.')).grid(row=0, column=0, sticky='w')
    options = ttk.Frame(inner)
    options.grid(row=1, column=0, sticky='w', pady=8)
    for column, (key, label) in enumerate((
            ('remove_soft_whitespace', 'Usuń miękkie entery'), ('normalize_formatting', 'Ujednolić formatowanie'),
            ('justify', 'Wyjustuj'), ('track_changes', 'Pobierz z zaznaczonymi zmianami'))):
        variables[key] = tk.BooleanVar(value=settings[key])
        ttk.Checkbutton(options, text=label, variable=variables[key]).grid(row=0, column=column, padx=(0, 14))
    buttons = ttk.Frame(inner)
    buttons.grid(row=2, column=0, sticky='w')
    rule_vars = {key: tk.BooleanVar(value=settings['rules'].get(key, True)) for key, _ in EDITORIAL_RULES}

    def set_rules(value):
        for variable in rule_vars.values():
            variable.set(value)

    def default_rules():
        for key, variable in rule_vars.items():
            variable.set(key in FORM_DEFAULT_EDITORIAL_RULES)

    ttk.Button(buttons, text='Włącz wszystkie', command=lambda: set_rules(True)).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text='Wyłącz wszystkie', command=lambda: set_rules(False)).grid(row=0, column=1, padx=(0, 8))
    ttk.Button(buttons, text='Domyślne', command=default_rules).grid(row=0, column=2)
    rules_frame = ttk.LabelFrame(inner, text='Reguły', padding=8)
    rules_frame.grid(row=3, column=0, sticky='ew', pady=8)
    rules_frame.columnconfigure(0, weight=1)
    for index, (key, label) in enumerate(EDITORIAL_RULES):
        ttk.Checkbutton(rules_frame, text=label, variable=rule_vars[key]).grid(row=index, column=0, sticky='w', pady=1)
    ttk.Label(inner, text='Słownik zamian (jedna pozycja w wierszu: „źródło = cel”; wyrażenie regularne: „re:wzorzec = cel”):').grid(
        row=4, column=0, sticky='w', pady=(8, 2))
    replacements_text_widget = tk.Text(inner, height=8, wrap='none', undo=True)
    replacements_text_widget.grid(row=5, column=0, sticky='ew')
    replacements_text_widget.insert('1.0', settings['replacements'])
    ttk.Button(inner, text='Przywróć wbudowany słownik', command=lambda: (
        replacements_text_widget.delete('1.0', 'end'),
        replacements_text_widget.insert('1.0', replacements_text(DEFAULT_REPLACEMENTS)))).grid(row=6, column=0, sticky='w', pady=4)

    # --- Konwersja ---
    tab = ttk.Frame(notebook, padding=14)
    notebook.add(tab, text='Konwersja')
    ttk.Label(tab, wraplength=880, justify='left', text=(
        'Konwersja DOCX na PDF i EPUB (czcionka Nimbus Roman, format A4, nagłówki i stopki z dokumentu). '
        'Pliki wynikowe trafiają obok źródła: „nazwa.pdf”, „nazwa.epub”. Formatowanie jest zawsze ujednolicane; '
        'reguły Odkurzacza i słownik zamian bierze z zakładki Odkurzacz.')).grid(row=0, column=0, sticky='w', pady=(0, 10))
    format_vars = {name: tk.BooleanVar(value=settings['formats'][name]) for name in ('pdf', 'epub')}
    ttk.Checkbutton(tab, text='PDF', variable=format_vars['pdf']).grid(row=1, column=0, sticky='w')
    ttk.Checkbutton(tab, text='EPUB', variable=format_vars['epub']).grid(row=2, column=0, sticky='w')
    for row, (key, label) in enumerate((('convert_clean', 'Zastosuj przed konwersją reguły Odkurzacza'),
                                        ('convert_rebuild', 'Przebuduj dokument przed konwersją (czysty DOCX)')), 3):
        variables[key] = tk.BooleanVar(value=settings[key])
        ttk.Checkbutton(tab, text=label, variable=variables[key]).grid(row=row, column=0, sticky='w')

    # --- Powtórzenia ---
    tab = ttk.Frame(notebook)
    notebook.add(tab, text='Powtórzenia')
    inner = scrollable(tab)
    inner.columnconfigure((0, 1), weight=1)
    ttk.Label(inner, wraplength=880, justify='left', text=(
        'Kolorowanie powtórzeń (model językowy pl_core_news_sm rozpoznaje odmianę) oraz dodatkowe kontrole: '
        'sąsiednie powtórzenia, długie zdania i akapity, puste nawiasy, własne słowa. '
        'Wynik: „nazwa_powtorzenia.docx”.')).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))
    numbers = ttk.Frame(inner)
    numbers.grid(row=1, column=0, columnspan=2, sticky='w')
    number_vars = {}
    for column, (key, label) in enumerate((('window_size', 'Zakres (słów):'), ('min_word_length', 'Min. długość słowa:'),
                                           ('sentence_limit', 'Długie zdanie – ponad:'), ('paragraph_limit', 'Długi akapit – ponad:'))):
        ttk.Label(numbers, text=label).grid(row=0, column=2 * column, padx=(0, 4))
        number_vars[key] = tk.StringVar(value=str(settings[key]))
        ttk.Entry(numbers, textvariable=number_vars[key], width=6).grid(row=0, column=2 * column + 1, padx=(0, 14))
    checks = ttk.Frame(inner)
    checks.grid(row=2, column=0, columnspan=2, sticky='w', pady=8)
    for index, (key, label) in enumerate((
            ('duplicates', 'Sąsiednie powtórzenia (zielone tło)'), ('long_sentences', 'Długie zdania (turkusowe tło)'),
            ('long_paragraphs', 'Długie akapity (szare tło)'), ('empty_pairs', 'Puste nawiasy i cudzysłowy (różowe tło)'),
            ('include_prefix_matches', 'Także słowa o wspólnych 5 pierwszych literach'))):
        variables[key] = tk.BooleanVar(value=settings[key])
        ttk.Checkbutton(checks, text=label, variable=variables[key]).grid(row=index // 2, column=index % 2, sticky='w', padx=(0, 14), pady=2)
    palette_var = tk.StringVar(value=settings['color_palette'])
    palette = ttk.Frame(inner)
    palette.grid(row=3, column=0, columnspan=2, sticky='w')
    ttk.Label(palette, text='Kolory liter:').grid(row=0, column=0, padx=(0, 8))
    ttk.Radiobutton(palette, text='ciemne (biała strona)', value='dark', variable=palette_var).grid(row=0, column=1)
    ttk.Radiobutton(palette, text='jasne (ciemny motyw)', value='light', variable=palette_var).grid(row=0, column=2, padx=(8, 0))
    ttk.Label(inner, text='Ignorowane słowa').grid(row=4, column=0, sticky='w', pady=(10, 2))
    ttk.Label(inner, text='Własne słowa do oznaczenia (żółte tło)').grid(row=4, column=1, sticky='w', pady=(10, 2))
    ignored_text = tk.Text(inner, height=8, wrap='word', undo=True)
    tracked_text = tk.Text(inner, height=8, wrap='word', undo=True)
    ignored_text.grid(row=5, column=0, sticky='nsew', padx=(0, 8))
    tracked_text.grid(row=5, column=1, sticky='nsew')
    ignored_text.insert('1.0', settings['ignored_words'])
    tracked_text.insert('1.0', settings['tracked_words'])

    # --- Przebudowa ---
    tab = ttk.Frame(notebook, padding=14)
    notebook.add(tab, text='Przebudowa')
    ttk.Label(tab, wraplength=880, justify='left', text=(
        'Przebudowa tworzy nowy, czysty DOCX z treścią i podstawowym formatowaniem źródła (bez ukrytych '
        'ustawień, kontrolek, pól i spisu treści z galerii). Elementy, których nie da się przenieść, są '
        'wymieniane przed przebudową. Wynik: „nazwa_przebudowa.docx”.')).grid(row=0, column=0, sticky='w', pady=(0, 10))
    for row, (key, label) in enumerate((('allow_omissions', 'Pozwól pominąć nieobsługiwane elementy'),
                                        ('keep_page_format', 'Zachowaj format strony (rozmiar, orientacja, marginesy, kolumny)')), 1):
        variables[key] = tk.BooleanVar(value=settings[key])
        ttk.Checkbutton(tab, text=label, variable=variables[key]).grid(row=row, column=0, sticky='w')

    # --- dół: uruchomienie, postęp ---
    bottom = ttk.Frame(root, padding=16)
    bottom.grid(row=2, column=0, sticky='ew')
    bottom.columnconfigure(0, weight=1)
    status_var = tk.StringVar(value=warning or 'Wybierz dokument, ustaw opcje i uruchom program z aktywnej zakładki.')
    run_button = ttk.Button(bottom, text='Uruchom program z aktywnej zakładki')
    run_button.grid(row=0, column=0, sticky='ew')
    progress = ttk.Progressbar(bottom, mode='indeterminate')
    progress.grid(row=1, column=0, sticky='ew', pady=(12, 6))
    ttk.Label(bottom, textvariable=status_var, wraplength=900, justify='left').grid(row=2, column=0, sticky='w')
    events = queue.Queue()
    busy = False

    def snapshot():
        current = dict(settings)
        current.update({key: variable.get() for key, variable in variables.items()})
        current['rules'] = {key: variable.get() for key, variable in rule_vars.items()}
        current['formats'] = {key: variable.get() for key, variable in format_vars.items()}
        current['color_palette'] = palette_var.get()
        current['strip_images'] = strip_var.get()
        current['open_result'] = open_var.get()
        current['replacements'] = replacements_text_widget.get('1.0', 'end-1c')
        current['ignored_words'] = ignored_text.get('1.0', 'end-1c')
        current['tracked_words'] = tracked_text.get('1.0', 'end-1c')
        for key, variable in number_vars.items():
            value = variable.get().strip()
            if value.isdigit() and int(value) > 0:
                current[key] = int(value)
        chosen = path_var.get().strip().strip('"')
        if chosen:
            current['last_directory'] = str(Path(chosen).parent)
        return current

    def persist():
        nonlocal settings
        try:
            settings = snapshot()
            save_settings(settings)
        except OSError as exc:
            status_var.set(f'Nie udało się zapamiętać ustawień: {exc}')

    def set_busy(value):
        nonlocal busy
        busy = value
        run_button.configure(state='disabled' if value else 'normal')
        browse.configure(state='disabled' if value else 'normal')
        if value:
            progress.start(12)
        else:
            progress.stop()

    def worker(program, source, current):
        try:
            result, notes = run_program(program, source, current, lambda text: events.put(('status', text)))
        except Exception as error:  # noqa: BLE001 – każdy błąd trafia do okna dialogowego
            message = str(error)
            if isinstance(error, (ImportError, ModuleNotFoundError)):
                message += ('\n\nZainstaluj zależności:\npip install python-docx lxml mammoth EbookLib fpdf2 Pillow '
                            'rapidfuzz spacy\npip install pl_core_news_sm==3.8.0 --find-links '
                            'https://github.com/explosion/spacy-models/releases/download/pl_core_news_sm-3.8.0/'
                            'pl_core_news_sm-3.8.0-py3-none-any.whl')
            events.put(('error', message))
        else:
            events.put(('done', (result, notes)))

    def start():
        if busy:
            return
        source = Path(path_var.get().strip().strip('"'))
        if not source.is_file() or source.suffix.lower() != '.docx':
            messagebox.showerror('Sprawdź plik', 'Wybierz istniejący plik DOCX.', parent=root)
            return
        persist()
        program = PROGRAMS[notebook.index(notebook.select())]
        set_busy(True)
        status_var.set(f'{program}: uruchamianie…')
        threading.Thread(target=worker, args=(program, str(source), dict(settings)), daemon=True).start()

    run_button.configure(command=start)

    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == 'status':
                    status_var.set(value)
                elif kind == 'done':
                    set_busy(False)
                    result, notes = value
                    targets = result if isinstance(result, list) else [result]
                    text = 'Zapisano: ' + ', '.join(str(target) for target in targets)
                    if notes:
                        text += '\n' + '\n'.join(notes)
                    status_var.set(text)
                    if open_var.get():
                        try:
                            open_result_file(targets[0])
                        except OSError as exc:
                            messagebox.showwarning('Plik został zapisany', f'Nie udało się otworzyć wyniku:\n{exc}', parent=root)
                    else:
                        messagebox.showinfo('Gotowe', text, parent=root)
                elif kind == 'error':
                    set_busy(False)
                    status_var.set('Przetwarzanie nie powiodło się. Możesz spróbować ponownie.')
                    messagebox.showerror('Błąd przetwarzania', value, parent=root)
        except queue.Empty:
            pass
        root.after(100, poll)

    def close():
        if busy:
            messagebox.showinfo('Trwa przetwarzanie', 'Poczekaj na zakończenie przetwarzania dokumentu.', parent=root)
            return
        persist()
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)
    root.after(100, poll)
    root.mainloop()


if __name__ == '__main__':
    launch_gui()
