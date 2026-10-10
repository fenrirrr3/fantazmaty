from django import forms
from django.db import transaction
from django.utils.html import format_html

from illustrations.models import Illustrator
from illustrations.contact_forms import identity_key
from .models import Anthology


class CoverArtistChoice(forms.ModelChoiceField):
    def label_from_instance(self, person):
        alias = f" ({person.pseudonym})" if person.pseudonym else ""
        inactive = " – nieaktywny" if not person.is_active else ""
        return f"{person}{alias}{inactive}"


def matching_artists(first_name, last_name):
    key = identity_key(f"{first_name} {last_name}")
    return [
        p
        for p in Illustrator.objects.all()
        if key in (identity_key(str(p)), identity_key(p.pseudonym))
    ]


class CoverAssignmentForm(forms.ModelForm):
    cover_illustrator = CoverArtistChoice(
        label="Przypisana osoba",
        queryset=Illustrator.objects.all(),
        required=False,
        widget=forms.Select(
            attrs={
                "data-searchable-person": "true",
                "data-search-placeholder": "Szukaj ilustratora, także nieaktywnego",
            }
        ),
    )
    new_cover_first_name = forms.CharField(
        label="Nowa osoba – imię",
        max_length=100,
        required=False,
        help_text="Uzupełnij tylko, gdy osoby nie ma w podpowiedziach. Dodamy nieaktywny wpis bez e-maila.",
    )
    new_cover_last_name = forms.CharField(
        label="Nowa osoba – nazwisko", max_length=100, required=False
    )
    clear_legacy_cover = forms.BooleanField(
        label="Usuń dotychczasowe przypisanie spoza spisu", required=False
    )

    class Meta:
        model = Anthology
        fields = (
            "cover_status",
            "cover_illustrator",
            "new_cover_first_name",
            "new_cover_last_name",
            "clear_legacy_cover",
            "cover_notes",
        )
        labels = {"cover_status": "Status"}
        widgets = {"cover_notes": forms.Textarea(attrs={"rows": 2})}

    class Media:
        js = ("core/person-select.js",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.legacy_name = (
            self.instance.cover_author if not self.instance.cover_illustrator_id else ""
        )
        if self.legacy_name:
            self.fields["cover_illustrator"].help_text = format_html(
                "Dotychczasowy zapis: {}. Wybierz osobę, aby połączyć wpis ze spisem.", self.legacy_name
            )
        elif "clear_legacy_cover" in self.fields:
            self.fields["clear_legacy_cover"].widget = forms.HiddenInput()
        self.new_artist = None

    def clean(self):
        data = super().clean()
        first = " ".join(data.get("new_cover_first_name", "").split())
        last = " ".join(data.get("new_cover_last_name", "").split())
        artist = data.get("cover_illustrator")
        if (first or last) and artist:
            raise forms.ValidationError(
                "Wybierz istniejącą osobę albo wpisz nową – nie obie jednocześnie."
            )
        if last and not first:
            self.add_error("new_cover_first_name", "Podaj imię nowej osoby.")
        if first:
            matches = matching_artists(first, last)
            if matches:
                self.add_error(
                    "new_cover_first_name",
                    "Taka nazwa lub pseudonim już jest w spisie. Wybierz właściwy wpis z wyszukiwarki.",
                )
            else:
                self.new_artist = Illustrator(
                    first_name=first, last_name=last, email=None, is_active=False, covers=True
                )
        legacy = self.legacy_name and not data.get("clear_legacy_cover")
        if data.get("clear_legacy_cover") and (artist or first):
            self.add_error(
                "clear_legacy_cover", "Aby wybrać nową osobę, odznacz usuwanie przypisania."
            )
        has_artist = bool(artist or self.new_artist or legacy)
        if data.get("cover_status") in ("in_progress", "ready") and not has_artist:
            self.add_error(
                "cover_illustrator", "Status Zlecone lub Gotowe wymaga wykonawcy okładki."
            )
        if data.get("cover_status") == "not_started" and has_artist:
            self.add_error("cover_status", "Przy przypisanej osobie wybierz Zlecone lub Gotowe.")
        return data

    def apply_artist(self, obj):
        if self.new_artist:
            # A repeat submission after a concurrent creation reuses the sole exact match.
            matches = matching_artists(self.new_artist.first_name, self.new_artist.last_name)
            if len(matches) > 1:
                raise forms.ValidationError(
                    "Powstało kilka pasujących wpisów. Odśwież stronę i wybierz osobę z listy."
                )
            if matches:
                artist = matches[0]
            else:
                artist = self.new_artist
                artist.full_clean()
                artist.save()
            obj.cover_illustrator = artist
        if obj.cover_illustrator_id:
            obj.cover_author = str(obj.cover_illustrator)
        else:
            obj.cover_author = (
                self.legacy_name if not self.cleaned_data.get("clear_legacy_cover") else ""
            )

    def save(self, commit=True):
        obj = super().save(commit=False)
        if commit:
            with transaction.atomic():
                self.apply_artist(obj)
                obj.save()
                self.save_m2m()
        return obj


class AnthologyAdminForm(CoverAssignmentForm):
    class Meta(CoverAssignmentForm.Meta):
        fields = "__all__"
