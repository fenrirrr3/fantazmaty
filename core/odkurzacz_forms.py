from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django import forms

from core.services.odkurzacz import EDITORIAL_RULES


class OdkurzaczForm(forms.Form):
    document = forms.FileField(
        label="Dokument DOCX",
        help_text="Maksymalnie 10 MB. Wynik pobierzesz jako osobny plik.",
        widget=forms.ClearableFileInput(attrs={"accept": ".docx"}),
    )
    rules = forms.MultipleChoiceField(
        label="Opcje korekty", choices=EDITORIAL_RULES,
        initial=[key for key, _ in EDITORIAL_RULES], required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def clean_document(self):
        upload = self.cleaned_data["document"]
        if Path(upload.name).suffix.lower() != ".docx":
            raise forms.ValidationError("Wybierz plik z rozszerzeniem .docx.")
        if upload.size > 10 * 1024 * 1024:
            raise forms.ValidationError("Plik jest za duży. Maksymalny rozmiar to 10 MB.")
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
