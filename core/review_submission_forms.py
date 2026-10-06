"""Submission validation shared by the public form and Django admin."""
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from core.normalization import NormalizedFormMixin, REVIEW_FIELDS
from texts.models import Review, Text, Anthology

class ReviewAdminForm(NormalizedFormMixin, forms.ModelForm):
    normalization_fields = REVIEW_FIELDS
    confirm_source_mismatch = forms.BooleanField(label='Potwierdzam rozbieżność tytułu lub autorów z wybranym tekstem', required=False)
    confirm_existing_text = forms.BooleanField(
        label="Potwierdzam powiązanie recenzji z wybranym istniejącym tekstem",
        required=False,
        help_text="Uwaga: upewnij się, że zgadzają się autor, tytuł i antologia. Powiązanie nie przenosi etapów ani nie nadpisuje danych tekstu.",
    )
    confirm_submission_warnings = forms.BooleanField(
        label="Zapoznałem się z ostrzeżeniami i potwierdzam zapis",
        required=False,
        help_text=(
            "Dotyczy ostrzeżenia o czarnej liście autora "
            "lub możliwym duplikacie zgłoszenia."
        ),
    )
    submission_warnings_token = forms.CharField(
        required=False,
        widget=forms.HiddenInput,
    )

    WARNING_TOKEN_SALT = "texts.admin.review-submission-warnings"

    class Meta:
        model = Review
        fields = (
            "author",
            "coauthors",
            "author_first_name",
            "author_last_name",
            "author_pseudonym",
            "title",
            "genre",
            "length",
            "content_warnings",
            "file_url",
            "email",
            "phone_number",
            "author_message",
            "anthology",
            "old_reviews",
        "is_hidden",
            "status",
            "author_notified_at",
            "decision_at",
            "copied_text",
            "publication_detached",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.submission_warnings = []
        if "copied_text" in self.fields:
            self.fields["copied_text"].queryset = Text.objects.filter(workflow_stages__isnull=False).distinct()
            self.fields["copied_text"].help_text = "Wyszukaj tekst już obecny w procesie wydawniczym albo użyj +, aby przygotować nowy na podstawie recenzji."

        if self.instance._state.adding and "anthology" in self.fields:
            self.fields["anthology"].queryset = Anthology.objects.filter(status=Anthology.Status.IN_PREPARATION).order_by("title", "pk")
        if 'anthology' in self.fields:
            self.fields['anthology'].queryset = self.fields['anthology'].queryset.filter(is_novel=False)

        # Dane zostaną uzupełnione po stronie serwera po wyborze autora.
        # Bez powiązanego autora pozostają wymagane w clean().
        for name in ("author_first_name", "author_last_name", "email"):
            if name in self.fields:
                self.fields[name].required = False

    def clean(self):
        cleaned_data = super().clean()
        if "old_reviews" not in self.fields:
            cleaned_data["old_reviews"] = self.instance.old_reviews
        linked = cleaned_data.get("copied_text")
        if linked and linked.pk != self.instance.copied_text_id:
            if not cleaned_data.get("confirm_existing_text"):
                self.add_error("confirm_existing_text", "Wybrany tekst już istnieje. Sprawdź powiązanie i zaznacz potwierdzenie przed zapisem.")
            if Review.objects.filter(copied_text=linked).exclude(pk=self.instance.pk).exists():
                self.add_error("copied_text", "Ten tekst jest już powiązany z inną recenzją.")
        author = cleaned_data.get("author")

        if author is not None:
            cleaned_data["author_first_name"] = author.first_name
            cleaned_data["author_last_name"] = author.last_name
            cleaned_data["author_pseudonym"] = cleaned_data.get("author_pseudonym") or author.pseudonym
            cleaned_data["email"] = author.email or ""
            if not author.email and not cleaned_data.get("old_reviews"):
                self.add_error("author", "Uzupełnij adres e-mail autora historycznego przed dodaniem nowego zgłoszenia.")
        else:
            for name in ("author_first_name", "author_last_name", "email"):
                if name in self.fields and not cleaned_data.get(name) and not (name == "email" and cleaned_data.get("old_reviews")):
                    self.add_error(
                        name,
                        "Uzupełnij dane albo wybierz autora z bazy.",
                    )

        link_fields = {'copied_text', 'title', 'author', 'coauthors', 'author_first_name', 'author_last_name', 'email', 'anthology', 'status'}
        if linked and not self.errors and (self.instance._state.adding or link_fields.intersection(self.changed_data)):
            from copy import copy
            from core.source_reviews import validate_source_review_link
            source = copy(self.instance)
            for key in ('title', 'status', 'anthology', 'author', 'author_first_name', 'author_last_name', 'email'):
                if key in cleaned_data:
                    setattr(source, key, cleaned_data[key])
            try:
                validate_source_review_link(linked, source, coauthors=cleaned_data.get('coauthors', ()),
                    confirm_mismatch=cleaned_data.get('confirm_source_mismatch', False))
            except ValidationError as error:
                self.add_error('copied_text', error)

        if self.errors:
            return cleaned_data

        identity_fields = {
            "author",
            "coauthors",
            "author_first_name",
            "author_last_name",
            "author_pseudonym",
            "title",
            "email",
            "anthology",
        }

        check_warnings = (
            self.instance._state.adding
            or bool(identity_fields.intersection(self.changed_data))
        )

        if not check_warnings:
            return cleaned_data

        # Wspólna funkcja dla admina i importu, implementowana
        # w texts/services.py. Zwraca listę komunikatów tekstowych
        # i uwzględnia także recenzje oznaczone old_reviews.
        from texts.services import get_review_submission_warnings

        self.submission_warnings = get_review_submission_warnings(
            author=author,
            author_first_name=cleaned_data["author_first_name"],
            author_last_name=cleaned_data["author_last_name"],
            email=cleaned_data["email"],
            title=cleaned_data["title"],
            exclude_review_id=self.instance.pk,
            anthology_id=getattr(cleaned_data.get("anthology"), "pk", None),
        )

        for coauthor in cleaned_data.get('coauthors', ()):
            warnings = get_review_submission_warnings(
                author=coauthor, email=coauthor.email, title=cleaned_data['title'],
                exclude_review_id=self.instance.pk,
                anthology_id=getattr(cleaned_data.get('anthology'), 'pk', None),
            )
            self.submission_warnings.extend(f'Współautor {coauthor.display_name}: {warning}' for warning in warnings)

        if not self.submission_warnings:
            return cleaned_data

        payload = {
            "review_id": self.instance.pk,
            "coauthor_ids": sorted(a.pk for a in cleaned_data.get("coauthors", ())),
            "author_id": author.pk if author is not None else None,
            "first_name": cleaned_data["author_first_name"],
            "last_name": cleaned_data["author_last_name"],
            "email": cleaned_data["email"],
            "title": cleaned_data["title"],
            "warnings": list(self.submission_warnings),
        }

        previous_payload = None
        token = cleaned_data.get("submission_warnings_token", "")

        if token:
            try:
                previous_payload = signing.loads(
                    token,
                    salt=self.WARNING_TOKEN_SALT,
                    max_age=3600,
                )
            except signing.BadSignature:
                pass

        confirmed = (
            cleaned_data.get("confirm_submission_warnings")
            and previous_payload == payload
        )

        if not confirmed:
            # Podpis wiąże potwierdzenie z uprzednio pokazanym
            # ostrzeżeniem oraz konkretnymi danymi zgłoszenia.
            token = signing.dumps(
                payload,
                salt=self.WARNING_TOKEN_SALT,
                compress=True,
            )
            self.data = self.data.copy()
            self.data[
                self.add_prefix("submission_warnings_token")
            ] = token
            self.data[
                self.add_prefix("confirm_submission_warnings")
            ] = ""

            self.add_error(
                "confirm_submission_warnings",
                " ".join(self.submission_warnings)
                + " Sprawdź zgłoszenie, zaznacz potwierdzenie "
                "i ponownie zapisz formularz.",
            )

        return cleaned_data


