from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django import forms

from core.services.odkurzacz import EDITORIAL_RULES, DEFAULT_EDITORIAL_RULES


PROGRAM_MAX_UPLOAD_BYTES = 2 * 1024 * 1024


class OdkurzaczForm(forms.Form):
    remove_soft_whitespace = forms.BooleanField(label="Usuń miękkie entery i spacje nieprzenoszące", required=False, initial=True)
    normalize_formatting = forms.BooleanField(label="Ujednolić formatowanie", required=False, initial=False)
    rebuild = forms.BooleanField(label="Przebuduj do nowego DOCX przed odkurzaniem", required=False, initial=False)
    document = forms.FileField(
        label="Dokument DOCX",
        help_text="Maksymalnie 2 MB. Wynik pobierzesz jako osobny plik.",
        widget=forms.ClearableFileInput(attrs={"accept": ".docx"}),
    )
    rules = forms.MultipleChoiceField(
        label="Opcje korekty", choices=EDITORIAL_RULES,
        initial=[key for key, _ in EDITORIAL_RULES if key in DEFAULT_EDITORIAL_RULES], required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, max_document_bytes=PROGRAM_MAX_UPLOAD_BYTES, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_document_bytes = max_document_bytes
        self.fields["document"].help_text = f"Maksymalnie {max_document_bytes // (1024 * 1024)} MB. Wynik pobierzesz jako osobny plik."

    def clean_document(self):
        upload = self.cleaned_data["document"]
        if Path(upload.name).suffix.lower() != ".docx":
            raise forms.ValidationError("Wybierz plik z rozszerzeniem .docx.")
        if upload.size > self.max_document_bytes:
            raise forms.ValidationError(f"Plik jest za duży. Maksymalny rozmiar to {self.max_document_bytes // (1024 * 1024)} MB.")
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


class DocumentConversionForm(OdkurzaczForm):
    formats = forms.MultipleChoiceField(
        label='Formaty docelowe', choices=(('pdf', 'PDF'), ('epub', 'EPUB')),
        widget=forms.CheckboxSelectMultiple, initial=['epub'],
        error_messages={'required': 'Wybierz co najmniej jeden format.'},
    )
    use_cleaner = forms.BooleanField(label='Użyj Odkurzacza przed konwersją', required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields['rules']
        del self.fields['rebuild']
        del self.fields['normalize_formatting']


class RepetitionsForm(OdkurzaczForm):
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields['remove_soft_whitespace']
        del self.fields['rules']
        del self.fields['rebuild']
        del self.fields['normalize_formatting']

    def analysis_config(self):
        values = self.cleaned_data
        return {key: values[key] for key in ('window_size', 'min_word_length', 'ignored_words',
                'tracked_words', 'include_prefix_matches')} | {'color_palette': values.get('color_palette') or 'dark', 'analysis_options': {
                key: values[key] for key in ('duplicates', 'long_sentences', 'sentence_limit',
                                            'long_paragraphs', 'paragraph_limit', 'empty_pairs')}}
