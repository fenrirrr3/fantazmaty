from core.translation_scope import ordinary
from workflow.catalog import active_role_choices
from django.db.models import Q
from people.role_ordering import ordered_team_roles
import hashlib
import json
import re
from datetime import datetime, time, timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.utils import timezone

from authors.models import AuthorNote
from people.models import Vacation, get_local_date
from texts.models import Anthology, Review, ReviewAssignment, Text, TextNote
from texts.services import (
    find_matching_authors,
    get_review_submission_warnings,
    normalize_email,
    normalize_review_title,
    normalize_whitespace,
)
from workflow.models import WorkflowRoleAssignment, WorkflowStage
from workflow.services import (
    RESTARTABLE_STAGE_TYPES,
    ROLE_GROUPS,
    validate_assignment_start_date,
)

from .permissions import has_role, is_coordinator
from .normalization import NormalizedFormMixin, TEXT_FIELDS


User = get_user_model()

from core.services.review_import_parser import (
    parse_review_records, MAX_IMPORT_RECORDS, MAX_IMPORT_CHARACTERS, MAX_IMPORT_ERRORS,
    REVIEW_IMPORT_LINE_PATTERN,
)


def date_widget():
    return forms.DateInput(
        format="%Y-%m-%d",
        attrs={"type": "date", "class": "stage-date-input"},
    )


def textarea_widget(css_class, rows=3, placeholder=""):
    return forms.Textarea(
        attrs={
            "class": css_class,
            "rows": rows,
            "placeholder": placeholder,
        }
    )


class CompleteStageForm(forms.Form):
    ended_at = forms.DateField(
        label="Data zakończenia",
        input_formats=("%Y-%m-%d", "%d.%m.%Y"),
        widget=date_widget(),
    )

    def __init__(self, *args, stage=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.stage = stage
        if stage is not None and stage.stage_type == 'editing_control':
            from workflow.decision_forms import editorial_decision_field
            self.fields['send_to_proofreading'] = editorial_decision_field()

        if stage is not None and stage.started_at:
            self.fields["ended_at"].widget.attrs["min"] = (
                stage.started_at.isoformat()
            )

    def clean_ended_at(self):
        ended_at = self.cleaned_data["ended_at"]
        if ended_at > timezone.localdate():
            raise ValidationError("Nie można zakończyć pracy z przyszłą datą.")

        if (
            self.stage is not None
            and self.stage.started_at
            and ended_at < self.stage.started_at
        ):
            raise ValidationError(
                "Data zakończenia nie może poprzedzać rozpoczęcia etapu."
            )

        return ended_at


class StartStageForm(forms.Form):
    started_at = forms.DateField(
        label="Data rozpoczęcia",
        input_formats=("%Y-%m-%d", "%d.%m.%Y"),
        widget=date_widget(),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate()

        if not self.initial.get("started_at"):
            self.initial["started_at"] = today

        self.fields["started_at"].widget.attrs.update(
            {
                "min": today.isoformat(),
                "max": (today + timedelta(days=14)).isoformat(),
            }
        )

    def clean_started_at(self):
        return validate_assignment_start_date(
            self.cleaned_data["started_at"]
        )


class RepeatPerformerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        return user.get_full_name() or user.get_username()


class RestartWorkflowForm(forms.Form):
    stages = forms.MultipleChoiceField(label="Etapy do powtórzenia", widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, text=None, **kwargs):
        super().__init__(*args, **kwargs)
        from workflow.repetitions import REPEATABLE, eligible_repeat_users
        from workflow.services import STAGE_ROLES
        self.text = text
        choices = [(kind, label) for kind, label in WorkflowStage.StageType.choices if kind in REPEATABLE]
        if text is not None:
            recorded = set(WorkflowStage.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle)
                           .exclude(repetition__canceled_at__isnull=False).values_list('stage_type', flat=True))
            choices = [(kind, label) for kind, label in choices if kind in recorded]
        self.fields['stages'].choices = choices
        self.performer_rows = []
        for role in dict.fromkeys(STAGE_ROLES[kind] for kind, _ in choices):
            name = f'performer_{role}'
            self.fields[name] = RepeatPerformerChoiceField(
                label=f'Nowa osoba: {dict(WorkflowRoleAssignment.Role.choices)[role]}',
                queryset=eligible_repeat_users(text, role) if text else User.objects.none(),
                required=False, empty_label='Wybierz osobę',
                widget=forms.Select(attrs={'data-searchable-person': 'true'}))
            self.performer_rows.append({
                'field': self[name],
                'stage_types': ','.join(kind for kind, _ in choices if STAGE_ROLES[kind] == role),
            })

    def clean(self):
        cleaned = super().clean()
        from workflow.services import STAGE_ROLES
        from workflow.repetitions import validate_repeat_assignees
        selected = cleaned.get('stages', [])
        roles = {STAGE_ROLES[kind] for kind in selected}
        assignees = {role: cleaned.get(f'performer_{role}') for role in roles}
        for role, performer in assignees.items():
            if performer is None and f'performer_{role}' not in self.errors:
                self.add_error(f'performer_{role}', 'Wybierz nową osobę dla tego wykonania.')
        if not self.errors and self.text:
            validate_repeat_assignees(self.text, selected, assignees)
        cleaned['assignees'] = assignees
        return cleaned


class VacationForm(forms.ModelForm):
    class Meta:
        model = Vacation
        fields = ("start_date", "end_date", "until_revoked")
        labels = {
            "start_date": "Data rozpoczęcia",
            "end_date": "Data zakończenia",
            "until_revoked": "Urlop do odwołania",
        }
        widgets = {
            "start_date": date_widget(),
            "end_date": forms.DateTimeInput(
                format="%Y-%m-%d",
                attrs={
                    "type": "date",
                    "class": "stage-date-input",
                    "step": "1",
                },
            ),
            "until_revoked": forms.CheckboxInput(
                attrs={"class": "vacation-checkbox"}
            ),
        }
        help_texts = {
            "end_date": "",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        today = timezone.localdate()
        self.minimum_start_date = today
        self.latest_allowed_date = today + timedelta(days=365)

        if (
            self.instance.pk
            and self.instance.start_date
            and self.instance.start_date < today
        ):
            self.minimum_start_date = self.instance.start_date

        self.fields["start_date"].input_formats = ("%Y-%m-%d",)
        self.fields["end_date"].input_formats = ("%Y-%m-%d", "%Y-%m-%dT%H:%M")

        if not self.instance.pk:
            self.initial["start_date"] = self.initial.get("start_date") or today
        self.fields["start_date"].widget.attrs.update(
            {
                "min": self.minimum_start_date.isoformat(),
                "max": self.latest_allowed_date.isoformat(),
            }
        )
        self.fields["end_date"].widget.attrs.update(
            {
                "min": today.isoformat(),
                "max": self.latest_allowed_date.isoformat(),
            }
        )

    def clean_end_date(self):
        value = self.cleaned_data.get("end_date")
        raw = self.data.get(self.add_prefix("end_date"), "")
        if value and isinstance(raw, str) and len(raw) == 10:
            return timezone.make_aware(datetime.combine(value.date(), time.max))
        return value

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")

        if cleaned_data.get("until_revoked"):
            cleaned_data["end_date"] = None
            end_date = None

        if start_date:
            if start_date < self.minimum_start_date:
                self.add_error(
                    "start_date",
                    "Data rozpoczęcia jest wcześniejsza "
                    "niż dozwolona data.",
                )

            if start_date > self.latest_allowed_date:
                self.add_error(
                    "start_date",
                    "Urlop można zgłosić najwyżej rok do przodu.",
                )

        if (
            end_date
            and get_local_date(end_date) > self.latest_allowed_date
        ):
            self.add_error(
                "end_date",
                "Data zakończenia nie może przypadać później "
                "niż rok od dzisiaj.",
            )

        if end_date and get_local_date(end_date) < timezone.localdate():
            self.add_error("end_date", "Data zakończenia nie może być wcześniejsza niż dzisiaj.")

        # Zgodność początku i końca oraz obowiązek podania końca
        # sprawdza również Vacation.clean().
        return cleaned_data


class ReviewerOpinionForm(NormalizedFormMixin, forms.Form):
    normalization_fields = TEXT_FIELDS
    opinion = forms.ChoiceField(
        label="Ocena",
        choices=(
            ("", "Wybierz ocenę"),
            *[
                (value, label)
                for value, label in ReviewAssignment.Opinion.choices
                if value != ReviewAssignment.Opinion.READING
            ],
        ),
        widget=forms.Select(attrs={"class": "review-opinion-select"}),
    )
    notes = forms.CharField(
        label="Uwagi / treść recenzji",
        widget=textarea_widget(
            "review-notes-input",
            rows=10,
            placeholder="Wklej tutaj treść swojej recenzji...",
        ),
    )
    content_warnings = forms.CharField(
        label="Trigger warningi",
        required=False,
        widget=textarea_widget(
            "content-warnings-input",
            placeholder="Dopisz lub uzupełnij trigger warningi...",
        ),
    )


class TextNoteForm(forms.ModelForm):
    class Meta:
        model = TextNote
        fields = ("content", "is_important")
        labels = {
            "content": "Nowa notatka",
            "is_important": "WAŻNE",
        }
        widgets = {
            "content": textarea_widget(
                "general-notes-input",
                placeholder="Wpisz notatkę dotyczącą tekstu...",
            ),
            "is_important": forms.CheckboxInput(
                attrs={"class": "important-note-checkbox"}
            ),
        }


class CoordinatorNoteForm(forms.ModelForm):
    class Meta:
        model = Text
        fields = ("coordinator_note",)
        labels = {"coordinator_note": "Notatka koordynatora"}
        widgets = {
            "coordinator_note": textarea_widget(
                "general-notes-input",
                placeholder="Uwaga dla osób pracujących przy tekście...",
            ),
        }


class TextContentWarningsForm(NormalizedFormMixin, forms.ModelForm):
    normalization_fields = TEXT_FIELDS
    class Meta:
        model = Text
        fields = ("content_warnings",)
        labels = {"content_warnings": "Trigger warningi"}
        widgets = {
            "content_warnings": textarea_widget(
                "content-warnings-input",
                placeholder="Wpisz trigger warningi dotyczące tekstu...",
            ),
        }


class ReviewContentWarningsForm(NormalizedFormMixin, forms.Form):
    """Validate only warnings, including reviews with incomplete imported metadata."""
    normalization_fields = TEXT_FIELDS
    content_warnings = forms.CharField(
        label="Trigger warningi", required=False,
        widget=textarea_widget("content-warnings-input", placeholder="Wpisz trigger warningi zauważone w tekście..."),
    )

    def __init__(self, *args, instance=None, **kwargs):
        if instance is not None:
            kwargs['initial'] = {"content_warnings": instance.content_warnings, **kwargs.get('initial', {})}
        super().__init__(*args, **kwargs)


class AuthorNotificationForm(forms.Form):
    author_notified = forms.BooleanField(
        label="Autor został powiadomiony o decyzji",
        required=False,
        widget=forms.CheckboxInput(
            attrs={"class": "author-notified-checkbox"}
        ),
    )
    author_notified_at = forms.DateField(
        label="Data powiadomienia autora",
        required=False,
        input_formats=("%Y-%m-%d", "%d.%m.%Y"),
        widget=date_widget(),
    )

    def __init__(self, *args, review=None, **kwargs):
        super().__init__(*args, **kwargs)

        today = timezone.localdate()
        if not self.initial.get("author_notified_at"):
            self.initial["author_notified_at"] = today
        self.fields["author_notified_at"].widget.attrs.update({
            "min": (today - timedelta(days=14)).isoformat(),
            "max": (today + timedelta(days=14)).isoformat(),
        })

        if review is not None and not self.is_bound:
            self.initial.update(
                {
                    "author_notified": review.author_notified_at is not None,
                    "author_notified_at": review.author_notified_at or timezone.localdate(),
                }
            )

    def clean(self):
        cleaned_data = super().clean()

        if cleaned_data.get("author_notified"):
            if (
                not cleaned_data.get("author_notified_at")
                and "author_notified_at" not in self.errors
            ):
                cleaned_data["author_notified_at"] = timezone.localdate()
        else:
            cleaned_data["author_notified_at"] = None

        return cleaned_data


    def clean_author_notified_at(self):
        value = self.cleaned_data.get("author_notified_at")
        today = timezone.localdate()
        if value and not today - timedelta(days=14) <= value <= today + timedelta(days=14):
            raise forms.ValidationError("Wybierz datę w zakresie 14 dni przed lub po dzisiejszym dniu.")
        return value


class AuthorNoteForm(forms.ModelForm):
    def clean_content(self):
        content = self.cleaned_data.get('content', '').strip()
        if not self.instance.pk and not content:
            raise ValidationError('Wpisz treść nowej notatki.')
        return content

    class Meta:
        model = AuthorNote
        fields = ("content",)
        labels = {"content": "Nowa notatka o autorze"}
        widgets = {
            "content": textarea_widget(
                "author-note-input",
                rows=4,
                placeholder="Wpisz wewnętrzną notatkę dotyczącą autora...",
            ),
        }


class ReviewBulkImportForm(forms.Form):
    """Waliduje dane i ostrzeżenia. Nie zapisuje zgłoszeń.

    Widok musi przekazać user=request.user.
    Zapis wykonuje serwis, który ponownie sprawdza uprawnienia
    i aktualny stan danych w transakcji.
    """

    WARNING_TOKEN_SALT = "core.review-bulk-import-warnings"

    anthology = forms.ModelChoiceField(
        label="Antologia",
        queryset=ordinary(Anthology.objects).filter(is_novel=False).filter(status=Anthology.Status.IN_PREPARATION).order_by("title", "pk"),
        empty_label="Wybierz antologię",
        widget=forms.Select(attrs={"class": "filter-select"}),
    )
    records = forms.CharField(
        label="Zgłoszenia",
        max_length=MAX_IMPORT_CHARACTERS,
        help_text="",
        widget=forms.Textarea(
            attrs={
                "class": "review-import-textarea",
                "rows": 16,
                "spellcheck": "false",
                "placeholder": 'Imię: Jan\nNazwisko: Kowalski\nPseudonim:\nTytuł opowiadania: Tytuł\nGatunek: fantasy\nOstrzeżenia o treści:\nLiczba znaków ze spacjami: 25000\nAdres e-mail: jan@example.com\nNumer telefonu:\nNewsletter: premierach, naborach\n\nWiadomość do redakcji:\n',
            }
        ),
    )
    confirm_submission_warnings = forms.BooleanField(
        label="Sprawdziłem ostrzeżenia i potwierdzam import",
        required=False,
    )
    submission_warnings_token = forms.CharField(
        required=False,
        widget=forms.HiddenInput,
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.parsed_records = []
        self.import_warnings = []
        self.warning_payload = None

    def _can_import(self):
        return bool(
            self.user
            and self.user.is_authenticated
            and self.user.is_active
            and self.user.is_superuser
        )

    def clean_records(self):
        if not self._can_import():
            raise ValidationError(
                "Import danych autorów jest dostępny wyłącznie "
                "dla superusera."
            )

        value = self.cleaned_data["records"]
        if hasattr(self, '_validated_records'):
            self.parsed_records = self._validated_records
            self.preview_records = self.parsed_records
            return value
        parsed_records, errors = parse_review_records(value)

        self.preview_records = parsed_records
        if errors:
            displayed_errors = errors[:MAX_IMPORT_ERRORS]

            if len(errors) > MAX_IMPORT_ERRORS:
                displayed_errors.append(
                    f"Pozostałych błędów: {len(errors) - MAX_IMPORT_ERRORS}. "
                    "Popraw dane i sprawdź je ponownie."
                )

            raise ValidationError(displayed_errors)

        self.parsed_records = parsed_records
        return value

    def clean(self):
        cleaned_data = super().clean()

        if not self._can_import():
            raise ValidationError(
                "Import danych autorów jest dostępny wyłącznie "
                "dla superusera."
            )

        if self.errors:
            return cleaned_data

        author_cache = {}
        warning_cache = {}
        seen_submissions = {}
        canonical_records = []

        for record in self.parsed_records:
            source = record.get('source_anthology', '')
            if source and normalize_whitespace(source).casefold() != normalize_whitespace(cleaned_data['anthology'].title).casefold():
                self.add_error('records', f"Wiersz {record['line_number']}: nabór „{source}” nie odpowiada wybranej antologii. Wybierz właściwą antologię.")
                continue
            email = record["email"]

            if email not in author_cache:
                matches = list(
                    find_matching_authors(email=email)[:2]
                )

                if len(matches) > 1:
                    self.add_error(
                        "records",
                        f"Wiersz {record['line_number']}: adres e-mail "
                        "pasuje do więcej niż jednego autora. "
                        "Najpierw uporządkuj rekordy autorów w adminie.",
                    )
                    continue

                author_cache[email] = matches[0] if matches else None

            author = author_cache[email]
            record["author_id"] = author.pk if author is not None else None

            identity = (
                f"author:{author.pk}"
                if author is not None
                else f"email:{email}"
            )
            title_key = normalize_review_title(record["title"])
            submission_key = (identity, title_key)

            if submission_key not in warning_cache:
                warning_cache[submission_key] = (
                    get_review_submission_warnings(
                        title=record["title"],
                        anthology_id=cleaned_data["anthology"].pk,
                        author=author,
                        author_first_name=record["author_first_name"],
                        author_last_name=record["author_last_name"],
                        email=email,
                    )
                )

            for warning in warning_cache[submission_key]:
                self.import_warnings.append(
                    f"Wiersz {record['line_number']}: {warning}"
                )

            if submission_key in seen_submissions:
                self.import_warnings.append(
                    f"Wiersz {record['line_number']}: możliwy duplikat "
                    f"wiersza {seen_submissions[submission_key]} "
                    "w tym samym imporcie."
                )
            else:
                seen_submissions[submission_key] = record["line_number"]

            canonical_records.append(record.copy())

        if self.errors or not self.import_warnings:
            return cleaned_data

        # Podpis nie zawiera pełnych danych osobowych ani treści importu.
        digest = hashlib.sha256(
            json.dumps(
                canonical_records,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        warnings_digest = hashlib.sha256(
            json.dumps(
                self.import_warnings,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        self.warning_payload = {
            "user_id": self.user.pk,
            "anthology_id": cleaned_data["anthology"].pk,
            "records_digest": digest,
            "warnings_digest": warnings_digest,
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
            and previous_payload == self.warning_payload
        )

        if not confirmed:
            self.data = self.data.copy()
            self.data[self.add_prefix("submission_warnings_token")] = (
                signing.dumps(
                    self.warning_payload,
                    salt=self.WARNING_TOKEN_SALT,
                    compress=True,
                )
            )
            self.data[self.add_prefix("confirm_submission_warnings")] = ""

            self.add_error(
                "confirm_submission_warnings",
                "Wykryto ostrzeżenia. Sprawdź listę, zaznacz "
                "potwierdzenie i ponownie zatwierdź import.",
            )

        return cleaned_data


class GlobalSearchForm(forms.Form):
    query = forms.CharField(
        label="Wyszukaj w CMS-ie",
        max_length=255,
        widget=forms.TextInput(
            attrs={"type": "search",
                "class": "global-search-input",
                "placeholder": "Tytuł, antologia lub osoba z zespołu",
                "autocomplete": "off",
                "enterkeyhint": "search",
            }
        ),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        if (
            user
            and user.is_authenticated
            and user.is_active
            and user.is_superuser
        ):
            self.fields["query"].widget.attrs["placeholder"] = (
                "Tytuł, autor, pseudonim, antologia, e-mail lub osoba"
            )


class PeopleFilterForm(forms.Form):
    query = forms.CharField(
        label="Szukaj w zespole",
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={"type": "search",
                "class": "filter-input",
                "placeholder": "Imię, nazwisko lub e-mail",
            }
        ),
    )
    roles = forms.ModelMultipleChoiceField(
        label="Role",
        queryset=ordered_team_roles(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Wyświetl osoby mające co najmniej jedną z wybranych ról.",
    )



class CoordinatorReviewBulkActionForm(forms.Form):
    class Action:
        EXPORT = "export"
        CHANGE_STATUS = "change_status"

    action = forms.ChoiceField(
        label="Operacja",
        choices=(
            ("", "Wybierz operację"),
            (Action.EXPORT, "Eksportuj zaznaczone do CSV"),
            (Action.CHANGE_STATUS, "Zmień status zgłoszeń"),
        ),
        widget=forms.Select(
            attrs={
                "class": "filter-select",
                "data-bulk-action-select": "",
            }
        ),
    )
    status = forms.ChoiceField(
        label="Nowy status",
        required=False,
        choices=(
            ("", "Wybierz status"),
            *Review.Status.choices,
        ),
        widget=forms.Select(
            attrs={
                "class": "filter-select",
                "data-bulk-field": "status",
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()

        if (
            cleaned_data.get("action") == self.Action.CHANGE_STATUS
            and not cleaned_data.get("status")
        ):
            self.add_error("status", "Wybierz nowy status zgłoszeń.")

        # Dozwolone przejście i uprawnienia muszą zostać sprawdzone
        # dla każdego zgłoszenia przez serwis wykonujący operację.
        return cleaned_data
