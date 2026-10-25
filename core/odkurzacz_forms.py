import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django import forms

from core.inflection import count_text
from core.program_models import ReplacementEntry
from core.services import odkurzacz
from core.services.document_errors import MESSAGES, foreign_format
from core.services.odkurzacz import EDITORIAL_RULES, FORM_DEFAULT_EDITORIAL_RULES, ORTHOGRAPHY_2026_KEYS


PROGRAM_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
FORM_DEFAULT_RULE_LIST = [key for key, _ in EDITORIAL_RULES if key in FORM_DEFAULT_EDITORIAL_RULES]


# Scalanie DOCX: najwięcej plików naraz i łączny rozmiar wysłanych plików.
MERGE_MAX_FILES = 30
MERGE_MAX_TOTAL_BYTES = 50 * 1024 * 1024
FILE_FORMS = ('plik', 'pliki', 'plików')


def validate_docx_upload(upload, max_bytes=PROGRAM_MAX_UPLOAD_BYTES):
    """Sprawdza wysłany DOCX (rozszerzenie, rozmiar, budowa ZIP); zwraca plik ustawiony na początek."""
    if Path(upload.name).suffix.lower() != ".docx":
        raise forms.ValidationError("Wybierz plik z rozszerzeniem .docx.")
    if upload.size > max_bytes:
        raise forms.ValidationError(f"Plik jest za duży. Maksymalny rozmiar to {max_bytes // (1024 * 1024)} MB.")
    # Inny format z rozszerzeniem .docx (np. .doc, ODT, DOCX z hasłem) albo odmiana DOCX, której programy nie czytają.
    upload.seek(0)
    kind = foreign_format(upload.read())
    upload.seek(0)
    if kind:
        raise forms.ValidationError(MESSAGES[kind])
    try:
        with ZipFile(upload) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(e.file_size for e in entries) > 50 * 1024 * 1024:
                raise forms.ValidationError("Dokument jest zbyt rozbudowany (limit zawartości: 50 MB).")
            if any(e.flag_bits & 1 for e in entries):
                raise forms.ValidationError("Dokument nie może być zaszyfrowany.")
            if not {"[Content_Types].xml", "word/document.xml"}.issubset(archive.namelist()):
                raise forms.ValidationError("Ten plik nie jest prawidłowym dokumentem DOCX.")
    except (BadZipFile, OSError, ValueError) as exc:
        raise forms.ValidationError("Nie można odczytać pliku. Wybierz prawidłowy DOCX.") from exc
    finally:
        upload.seek(0)
    return upload


def rules_field(label="Opcje korekty"):
    return forms.MultipleChoiceField(
        label=label, choices=EDITORIAL_RULES, initial=list(FORM_DEFAULT_RULE_LIST), required=False,
        widget=forms.CheckboxSelectMultiple,
    )


class ProgramSettingsMixin:
    """Ustawienia formularza do zapamiętania (bez pliku) i ich bezpieczny odczyt."""

    settings_exclude = ('document',)
    # Pola wielokrotnego wyboru, przy których zapamiętujemy też listę znanych wtedy opcji: opcja dodana
    # później (np. nowa reguła Odkurzacza) jest zaznaczona tak jak domyślnie, a nie zawsze odznaczona.
    settings_known_choices = ('rules',)
    # Zapis sprzed listy znanych opcji znał wszystkie opcje poza dodanymi później (reguły „Ortografia 2026”).
    settings_later_choices = {'rules': ORTHOGRAPHY_2026_KEYS}

    def settings_fields(self):
        return [name for name in self.fields if name not in self.settings_exclude]

    def settings_values(self):
        values = {name: self.cleaned_data[name] for name in self.settings_fields() if name in self.cleaned_data}
        for name in self.settings_known_choices:
            if name in values:
                values[f'known_{name}'] = [str(key) for key, _ in self.fields[name].choices]
        return values

    @classmethod
    def initial_from_settings(cls, stored, **kwargs):
        """Sprawdzone wartości zapisanych ustawień; nieznane klucze i wartości są pomijane."""
        if not isinstance(stored, dict):
            return {}
        form = cls(**kwargs)
        initial = {}
        for name in form.settings_fields():
            if name not in stored:
                continue
            field, value = form.fields[name], stored[name]
            if isinstance(field, forms.MultipleChoiceField):
                if not isinstance(value, list):
                    continue
                allowed = {str(key) for key, _ in field.choices}
                value = [item for item in value if isinstance(item, str) and item in allowed]
                if name in cls.settings_known_choices:
                    known = stored.get(f'known_{name}')
                    if not isinstance(known, list):
                        known = allowed - set(cls.settings_later_choices.get(name, ()))
                    value += [str(key) for key, _ in field.choices if str(key) not in known
                              and str(key) in map(str, field.initial or ()) and str(key) not in value]
            elif isinstance(field, forms.BooleanField):
                if not isinstance(value, bool):
                    continue
            elif isinstance(field, forms.IntegerField):
                if not isinstance(value, int) or isinstance(value, bool):
                    continue
            elif not isinstance(value, str):
                continue
            try:
                initial[name] = field.clean(value)
            except forms.ValidationError:
                continue
        return initial


class ProgramForm(ProgramSettingsMixin, forms.Form):
    """Formularz programu z zapamiętywanymi ustawieniami (limit wysyłanego DOCX: PROGRAM_MAX_UPLOAD_BYTES)."""


class ProgramDocumentForm(ProgramForm):
    """Formularz programu z jednym plikiem DOCX."""

    document = forms.FileField(label="Dokument DOCX", widget=forms.ClearableFileInput(attrs={"accept": ".docx"}))
    document_help = ("Maksymalnie {mb} MB. Obrazy są usuwane z dokumentu przed pracą programu. "
                     "Wynik pobierzesz jako osobny plik.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["document"].help_text = self.document_help.format(mb=PROGRAM_MAX_UPLOAD_BYTES // (1024 * 1024))

    def clean_document(self):
        return validate_docx_upload(self.cleaned_data["document"])


class CleanerOptionsForm(forms.Form):
    """Opcje Odkurzacza wspólne dla Odkurzacza i scalania DOCX."""

    remove_soft_whitespace = forms.BooleanField(label="Usuń miękkie entery i spacje nieprzenoszące", required=False, initial=True)
    normalize_formatting = forms.BooleanField(label="Ujednolić formatowanie", required=False, initial=False)
    keep_page_format = forms.BooleanField(
        label="Przy przebudowie zachowaj format strony", required=False, initial=False,
        help_text="Nowy DOCX dostanie rozmiar strony, orientację, marginesy i kolumny pierwszej sekcji oryginału. "
                  "Działa tylko razem z przebudową; ujednolicenie formatowania i tak ustawi A4.")
    track_changes = forms.BooleanField(
        label="Pobierz z zaznaczonymi zmianami (śledzenie zmian Worda)", required=False, initial=False,
        help_text="Poprawki reguł korekty będą widoczne w Wordzie jako śledzone zmiany do przejrzenia. "
                  "Zamiana miękkich enterów, przebudowa i ujednolicenie formatowania nie są oznaczane.")
    cleaner_summary = forms.BooleanField(
        label="Pokaż podsumowanie zmian", required=False, initial=False,
        help_text="Po zakończeniu pracy zobaczysz listę reguł z liczbą wprowadzonych zmian "
                  "oraz zamiany ze słownika: co na co i ile razy.")
    rules = rules_field()


class OdkurzaczForm(CleanerOptionsForm, ProgramDocumentForm):
    rebuild = forms.BooleanField(label="Przebuduj do nowego DOCX przed odkurzaniem", required=False, initial=False)


class DocumentConversionForm(ProgramDocumentForm):
    # Miękkie entery usuwa Odkurzacz – w konwerterze działa to razem z „Użyj Odkurzacza”.
    rules = rules_field('Reguły Odkurzacza')
    formats = forms.MultipleChoiceField(
        label='Formaty docelowe', choices=(('pdf', 'PDF'), ('epub', 'EPUB')),
        widget=forms.CheckboxSelectMultiple, initial=['epub'],
        error_messages={'required': 'Wybierz co najmniej jeden format.'},
    )
    use_cleaner = forms.BooleanField(label='Użyj Odkurzacza przed konwersją', required=False)
    # Strona zawsze wysyła ten znacznik razem z listą reguł. Bez niego (starszy
    # formularz, wywołanie bez listy) Odkurzacz użyje reguł domyślnych.
    rules_shown = forms.BooleanField(required=False, initial=True, widget=forms.HiddenInput)

    settings_exclude = ('document', 'rules_shown')

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('rules') and not cleaned.get('rules_shown') and 'rules' not in self.errors:
            cleaned['rules'] = list(FORM_DEFAULT_RULE_LIST)
        return cleaned


class RepetitionsForm(ProgramDocumentForm):
    color_palette = forms.ChoiceField(label='Kolory powtórzeń', choices=[('dark', 'Ciemne'), ('light', 'Jasne')],
        initial='dark', required=False, help_text='Paleta kolorów liter. Kolory tła dodatkowych oznaczeń pozostają bez zmian.')
    window_size = forms.IntegerField(label='Zakres wyszukiwania (słowa)', initial=35, min_value=1, max_value=500)
    min_word_length = forms.IntegerField(label='Minimalna długość słowa', initial=4, min_value=1, max_value=100)
    ignored_words = forms.CharField(label='Ignorowane słowa', required=False, max_length=10000,
                                   widget=forms.Textarea(attrs={'rows': 4}))
    tracked_words = forms.CharField(label='Własne słowa do oznaczenia', required=False, max_length=10000,
                                   widget=forms.Textarea(attrs={'rows': 4}))
    include_prefix_matches = forms.BooleanField(label='Także podobny początek słowa (pierwsze pięć liter)', required=False)
    duplicates = forms.BooleanField(label='Sąsiednie powtórzenia – zielone tło', required=False, initial=True)
    long_sentences = forms.BooleanField(label='Długie zdania – turkusowe tło', required=False, initial=True)
    sentence_limit = forms.IntegerField(label='Próg długiego zdania (słowa)', initial=35, min_value=1, max_value=10000)
    long_paragraphs = forms.BooleanField(label='Długie akapity – szare tło', required=False, initial=True)
    paragraph_limit = forms.IntegerField(label='Próg długiego akapitu (słowa)', initial=150, min_value=1, max_value=10000)
    empty_pairs = forms.BooleanField(label='Puste nawiasy i cudzysłowy – różowe tło', required=False, initial=True)

    def analysis_config(self):
        values = self.cleaned_data
        return {key: values[key] for key in ('window_size', 'min_word_length', 'ignored_words',
                'tracked_words', 'include_prefix_matches')} | {'color_palette': values.get('color_palette') or 'dark', 'analysis_options': {
                key: values[key] for key in ('duplicates', 'long_sentences', 'sentence_limit',
                                            'long_paragraphs', 'paragraph_limit', 'empty_pairs')}}


class MultipleDocxInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleDocxField(forms.FileField):
    """Kilka plików z jednego pola (dokumentacja Django: „Uploading multiple files”); wynik to lista.

    Pole jest zawsze wymagane (scalanie bez plików nie ma sensu).
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleDocxInput(attrs={"accept": ".docx"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        files = [item for item in (data if isinstance(data, (list, tuple)) else [data]) if item]
        if not files:
            raise forms.ValidationError(self.error_messages["required"], code="required")
        return [super(MultipleDocxField, self).clean(item, initial) for item in files]


class MergeForm(CleanerOptionsForm, ProgramForm):
    """Scalanie DOCX: kilka plików w kolejności wyboru i opcje Odkurzacza (przebudowa zawsze włączona)."""

    documents = MultipleDocxField(label="Dokumenty DOCX (w kolejności scalania)")
    use_template = forms.BooleanField(
        label="Użyj stylów szablonu Fantazmatów", required=False, initial=False,
        help_text="Scalony dokument dostaje wszystkie style z szablonu TEKST Szablon 10-2025 (np. Podstawowy tekst, "
                  "Gwiazdki, Cytat (motto)), żeby można było je od razu nadawać w Wordzie. Treść i jej formatowanie "
                  "pozostają jak po przebudowie; z szablonu nie jest przenoszone nic poza stylami.")
    settings_exclude = ("documents",)
    field_order = ("documents",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["documents"].help_text = (
            f"Od 2 do {MERGE_MAX_FILES} plików, każdy do {PROGRAM_MAX_UPLOAD_BYTES // (1024 * 1024)} MB, "
            f"razem do {MERGE_MAX_TOTAL_BYTES // (1024 * 1024)} MB. Kolejność scalania = kolejność wyboru plików.")
        self.fields["keep_page_format"].help_text = (
            "Wynik dostanie rozmiar strony, orientację, marginesy i kolumny pierwszego pliku. "
            "Ujednolicenie formatowania i tak ustawi A4.")

    def clean_documents(self):
        files = self.cleaned_data["documents"]
        if len(files) < 2:
            raise forms.ValidationError("Wybierz co najmniej dwa pliki DOCX do scalenia.")
        if len(files) > MERGE_MAX_FILES:
            raise forms.ValidationError(f"Wybrano {count_text(len(files), FILE_FORMS)}. "
                                        f"Naraz można scalić najwyżej {MERGE_MAX_FILES} plików.")
        if sum(item.size for item in files) > MERGE_MAX_TOTAL_BYTES:
            raise forms.ValidationError(f"Wybrane pliki są razem za duże. Łączny limit to "
                                        f"{MERGE_MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
        errors = []
        for item in files:
            try:
                validate_docx_upload(item)
            except forms.ValidationError as error:
                errors.extend(f"„{Path(item.name).name}”: {message}" for message in error.messages)
        if errors:
            raise forms.ValidationError(errors)
        return files


class SplitForm(ProgramDocumentForm):
    """Podział DOCX według Nagłówków 1 – bez opcji: tylko pole pliku."""

    document_help = "Plik do {mb} MB. Wynik pobierzesz jako archiwum ZIP z plikami DOCX."


REPLACEMENT_WORD_MAX = 100
WORD_SOURCE_RE = re.compile(r"[\w’'\-. ]+")
EMPTY_MATCH_SAMPLES = ('', ' ', 'a', 'Ala ma kota.', 'ąę – „x”')
# Odkurzacz bierze najwyżej tyle aktywnych pozycji słownika (dalsze byłyby po cichu pominięte).
REPLACEMENT_ACTIVE_MAX = odkurzacz.MAX_REPLACEMENTS


class ReplacementEntryForm(forms.ModelForm):
    """Pozycja słownika zamian. Wyrażenia regularne może dodawać i zmieniać tylko superuser."""

    class Meta:
        model = ReplacementEntry
        fields = ('kind', 'source', 'target', 'is_active', 'position', 'note')
        widgets = {'source': forms.TextInput(attrs={'spellcheck': 'false', 'autocomplete': 'off'}),
                   'target': forms.TextInput(attrs={'spellcheck': 'false', 'autocomplete': 'off'})}
        help_texts = {
            'source': 'Słowo: całe słowo, bez względu na wielkość liter. '
                      'Wyrażenie regularne: składnia Pythona, bez rozróżniania wielkości liter.',
            'target': 'Zamiana zapisana małymi literami przejmuje wielkość liter z tekstu (Mentlik → Mętlik, '
                      'MENTLIK → MĘTLIK). Zamiana z wielkimi literami (NATO, iPhone) zostaje zapisana dokładnie tak, '
                      'chyba że w tekście całe słowo jest napisane WIELKIMI literami. '
                      'We wzorcu można użyć grup: \\g<nazwa>.',
            'position': 'Mniejsza liczba – wcześniejsze wykonanie. Słowa są zamieniane jednocześnie, jednym '
                        'przejściem: wynik jednej zamiany nie jest ponownie zamieniany przez inną pozycję '
                        '(a → b i b → c nie daje c), a gdy zapisy się nakładają, wygrywa dłuższy. '
                        'Wyrażenia regularne działają po kolei, każde na tekście po wcześniejszych pozycjach.',
        }

    def __init__(self, *args, allow_patterns=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_patterns = allow_patterns
        if not allow_patterns:
            self.fields['kind'].choices = [(value, label) for value, label in self.fields['kind'].choices
                                           if value == ReplacementEntry.Kind.WORD]

    def clean_source(self):
        return self.cleaned_data['source'].strip()

    def clean_target(self):
        value = self.cleaned_data['target']
        if any(ord(char) < 32 for char in value):
            raise forms.ValidationError('Zamiana nie może zawierać znaków sterujących.')
        return value

    def clean(self):
        cleaned = super().clean()
        kind, source, target = cleaned.get('kind'), cleaned.get('source'), cleaned.get('target')
        if kind == ReplacementEntry.Kind.PATTERN and not self.allow_patterns:
            raise forms.ValidationError('Wyrażenia regularne może dodawać i zmieniać tylko superuser.')
        if not kind or not source:
            return cleaned
        if kind == ReplacementEntry.Kind.WORD:
            if len(source) > REPLACEMENT_WORD_MAX:
                self.add_error('source', f'Słowo może mieć najwyżej {REPLACEMENT_WORD_MAX} znaków.')
            elif not WORD_SOURCE_RE.fullmatch(source) or not any(char.isalpha() for char in source):
                self.add_error('source', 'Słowo może zawierać litery, cyfry, spacje, łączniki, kropki i apostrofy.')
        else:
            try:
                compiled = re.compile(source, re.IGNORECASE)
            except (re.error, RecursionError, OverflowError) as error:
                self.add_error('source', f'Nieprawidłowe wyrażenie regularne: {error}.')
                compiled = None
            if compiled is not None and any(match.start() == match.end() for sample in EMPTY_MATCH_SAMPLES
                                            for match in compiled.finditer(sample)):
                self.add_error('source', 'Wzorzec nie może pasować do pustego tekstu.')
            elif compiled is not None and target is not None:
                try:
                    compiled.sub(target, '')  # sprawdza odwołania do grup i znaki ucieczki w zamianie
                except (re.error, IndexError) as error:
                    self.add_error('target', f'Nieprawidłowa zamiana: {error}.')
                else:
                    # Ta sama kontrola co w Odkurzaczu: wzorzec odrzucony tam (błędny albo zbyt kosztowny)
                    # byłby po cichu pomijany.
                    if odkurzacz._compile_pattern(source, target) is None:
                        self.add_error('source', 'Odkurzacz nie użyje tego wzorca: może działać bardzo długo '
                                                 '(np. zagnieżdżone powtórzenia jak (a+)+ albo \\w*\\w*). '
                                                 'Uprość wyrażenie.')
        if cleaned.get('is_active') and (self.instance.pk is None or not self.instance.is_active):
            active = ReplacementEntry.objects.filter(is_active=True).exclude(pk=self.instance.pk).count()
            if active >= REPLACEMENT_ACTIVE_MAX:
                self.add_error(None, f'Słownik ma już {REPLACEMENT_ACTIVE_MAX} aktywnych pozycji – tyle '
                                     'najwięcej używa Odkurzacz. Wyłącz lub usuń niepotrzebne pozycje albo dodaj '
                                     'tę jako wyłączoną.')
        duplicates = ReplacementEntry.objects.filter(kind=kind).exclude(pk=self.instance.pk).only('source')
        if any(entry.source.casefold() == source.casefold() for entry in duplicates):
            self.add_error('source', 'Taka pozycja już jest w słowniku.')
        return cleaned
