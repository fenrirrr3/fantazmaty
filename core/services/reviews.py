from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from authors.models import Author
from core.permissions import (
    can_manage_reviews,
    can_self_assign_reviews,
    is_superuser,
    require_coordinator,
    require_superuser,
    require_team_member,
)
from texts.models import (
    Anthology,
    Review,
    ReviewAssignment,
    Reviewers,
    Text,
)
from texts.services import (
    normalize_author_name,
    normalize_email,
)
from workflow.models import WorkflowStage


MAX_BULK_REVIEWS = 1000
MAX_DATABASE_ID = 9_223_372_036_854_775_807

OPEN_STATUSES = frozenset(
    {
        Review.Status.NEW,
        Review.Status.IN_REVIEW,
    }
)
DECISION_STATUSES = frozenset(
    {
        Review.Status.ACCEPTED,
        Review.Status.REJECTED,
        Review.Status.WITHDRAWN,
    }
)


def _lock_review(review_id):
    # Nie łączymy select_for_update z opcjonalnymi relacjami.
    return get_object_or_404(
        Review.objects.select_for_update(),
        pk=review_id,
    )


def _lock_assignments(review):
    return list(
        ReviewAssignment.objects.select_for_update()
        .filter(review_id=review.pk)
        .order_by("position", "pk")
    )


def _require_current_review(review):
    if review.old_reviews:
        raise ValidationError("Archiwalna recenzja jest zablokowana.")


def _require_open_review(review):
    _require_current_review(review)
    if review.is_hidden:
        raise PermissionDenied("Ukryte zgłoszenie nie jest dostępne do recenzowania.")

    if review.status not in OPEN_STATUSES:
        raise ValidationError(
            "Po podjęciu ostatecznej decyzji recenzja jest zablokowana."
        )

    if review.copied_text_id is not None:
        raise ValidationError(
            "Zgłoszenie zostało już przeniesione do procesu wydawniczego."
        )


def _own_assignment(assignments, user):
    return next(
        (
            assignment
            for assignment in assignments
            if assignment.user_id == user.pk
        ),
        None,
    )


def _require_own_assignment(assignments, user):
    assignment = _own_assignment(assignments, user)

    if assignment is None:
        raise PermissionDenied(
            "Nie jesteś przypisanym recenzentem tego zgłoszenia."
        )

    return assignment


def _validate_form(form):
    if not form.is_valid():
        raise ValidationError(
            [
                str(error)
                for errors in form.errors.values()
                for error in errors
            ]
        )

    return form.cleaned_data


def _normalise_review_ids(review_ids):
    if review_ids is None or isinstance(review_ids, (str, bytes)):
        raise ValidationError("Przesłano nieprawidłową listę zgłoszeń.")

    result = set()

    for index, review_id in enumerate(review_ids):
        if index >= MAX_BULK_REVIEWS:
            raise ValidationError(
                f"Jednorazowo można zmienić najwyżej "
                f"{MAX_BULK_REVIEWS} zgłoszeń."
            )

        if (
            type(review_id) is not int
            or not 1 <= review_id <= MAX_DATABASE_ID
        ):
            raise ValidationError("Przesłano nieprawidłowy identyfikator.")

        result.add(review_id)

    if not result:
        raise ValidationError("Zaznacz przynajmniej jedno zgłoszenie.")

    return sorted(result)


@transaction.atomic
def assign_reviewer(*, user, review_id):
    require_team_member(user)

    if not can_self_assign_reviews(user):
        raise PermissionDenied(
            "Do recenzji mogą przypisywać się osoby z rolą recenzenta."
        )

    review = _lock_review(review_id)
    _require_open_review(review)
    assignments = _lock_assignments(review)

    existing = _own_assignment(assignments, user)
    if existing is not None:
        return existing

    occupied_positions = {
        assignment.position
        for assignment in assignments
    }
    position = next(
        (
            value
            for value in range(1, ReviewAssignment.MAX_REVIEWERS + 1)
            if value not in occupied_positions
        ),
        None,
    )

    # Przydział z usuniętym kontem nadal zajmuje swoją pozycję.
    if (
        position is None
        or len(assignments) >= ReviewAssignment.MAX_REVIEWERS
    ):
        raise ValidationError("Zgłoszenie ma już komplet recenzentów.")

    assignment = ReviewAssignment(
        review=review,
        user=user,
        position=position,
        opinion=Reviewers.Opinion.READING,
        notes="",
    )
    assignment.full_clean()
    assignment.save()

    if review.status == Review.Status.NEW:
        review.status = Review.Status.IN_REVIEW
        review.save(update_fields=["status"])

    return assignment


@transaction.atomic
def unassign_reviewer(*, user, review_id):
    require_team_member(user)

    review = _lock_review(review_id)
    _require_open_review(review)
    assignments = _lock_assignments(review)
    assignment = _own_assignment(assignments, user)

    if assignment is None:
        return False

    assignment.delete()

    if len(assignments) == 1 and review.status == Review.Status.IN_REVIEW:
        review.status = Review.Status.NEW
        review.save(update_fields=["status"])

    return True


@transaction.atomic
def save_reviewer_opinion(
    *,
    user,
    review_id,
    opinion,
    notes,
    content_warnings="",
):
    from core.forms import ReviewerOpinionForm

    require_team_member(user)
    review = _lock_review(review_id)
    _require_open_review(review)
    assignment = _require_own_assignment(_lock_assignments(review), user)

    cleaned = _validate_form(
        ReviewerOpinionForm(
            {
                "opinion": opinion,
                "notes": notes,
                "content_warnings": content_warnings,
            }
        )
    )

    update_fields = ["opinion", "notes"]
    if assignment.opinion != cleaned["opinion"]:
        assignment.opinion_changed_at = timezone.localdate()
        update_fields.append("opinion_changed_at")

    assignment.opinion = cleaned["opinion"]
    assignment.notes = cleaned["notes"]
    assignment.full_clean()
    assignment.save(update_fields=update_fields)

    review.content_warnings = cleaned["content_warnings"]
    review.full_clean()
    review.save(update_fields=["content_warnings"])

    return assignment


@transaction.atomic
def save_review_content_warnings(*, user, review_id, content_warnings):
    from core.forms import ReviewContentWarningsForm

    require_team_member(user)
    review = _lock_review(review_id)
    _require_open_review(review)
    assignments = _lock_assignments(review)

    if not can_manage_reviews(user):
        _require_own_assignment(assignments, user)

    form = ReviewContentWarningsForm(
        {"content_warnings": content_warnings},
        instance=review,
    )
    cleaned = _validate_form(form)

    review.content_warnings = cleaned["content_warnings"]
    review.save(update_fields=["content_warnings"])
    return review


def _validate_status_change(*, user, review, assignments, new_status):
    if review.is_hidden and not is_superuser(user):
        raise PermissionDenied
    _require_current_review(review)

    valid_statuses = {value for value, _label in Review.Status.choices}
    if new_status not in valid_statuses:
        raise ValidationError("Wybrano nieprawidłowy status.")

    if not is_superuser(user):
        if new_status not in {Review.Status.ACCEPTED, Review.Status.REJECTED}:
            raise PermissionDenied(
                "Ten status może ustawić wyłącznie superuser."
            )
        if review.status == Review.Status.WITHDRAWN:
            raise PermissionDenied(
                "Wycofaną recenzję może ponownie otworzyć wyłącznie superuser."
            )

    if review.copied_text_id is not None:
        raise ValidationError(
            "Nie można zmienić decyzji po przeniesieniu tekstu "
            "do procesu wydawniczego."
        )

    if new_status == Review.Status.NEW and assignments:
        raise ValidationError(
            "Zgłoszenie z przypisanymi recenzentami nie może mieć statusu nowego."
        )

    if new_status == Review.Status.IN_REVIEW and not assignments:
        raise ValidationError(
            "Status recenzowania wymaga przynajmniej jednego przydziału."
        )


def _apply_status_change(review, new_status, *, today):
    if review.status == new_status:
        return review

    review.status = new_status
    review.decision_at = today if new_status in DECISION_STATUSES else None

    # Poprzednie powiadomienie nie potwierdza przekazania nowej decyzji.
    review.author_notified_at = None
    review.full_clean()
    review.save(
        update_fields=[
            "status",
            "decision_at",
            "author_notified_at",
        ]
    )
    return review


@transaction.atomic
def change_review_status(*, user, review_id, new_status):
    require_coordinator(user)
    review = _lock_review(review_id)
    assignments = _lock_assignments(review)

    _validate_status_change(
        user=user,
        review=review,
        assignments=assignments,
        new_status=new_status,
    )

    return _apply_status_change(
        review,
        new_status,
        today=timezone.localdate(),
    )


@transaction.atomic
def perform_bulk_review_action(
    *,
    user,
    review_ids,
    action,
    new_status,
):
    require_superuser(user)

    if action != "change_status":
        raise ValidationError("Nieprawidłowa operacja zbiorcza.")

    selected_ids = _normalise_review_ids(review_ids)
    reviews = list(
        Review.objects.select_for_update()
        .filter(pk__in=selected_ids)
        .order_by("pk")
    )

    if len(reviews) != len(selected_ids):
        raise ValidationError(
            "Nie odnaleziono wszystkich zaznaczonych zgłoszeń. "
            "Żadna zmiana nie została zapisana."
        )

    assignments_by_review = {review.pk: [] for review in reviews}

    for assignment in (
        ReviewAssignment.objects.select_for_update()
        .filter(review_id__in=selected_ids)
        .order_by("review_id", "position", "pk")
    ):
        assignments_by_review[assignment.review_id].append(assignment)

    for review in reviews:
        _validate_status_change(
            user=user,
            review=review,
            assignments=assignments_by_review[review.pk],
            new_status=new_status,
        )

    today = timezone.localdate()
    for review in reviews:
        _apply_status_change(review, new_status, today=today)

    return len(reviews)


@transaction.atomic
def save_author_notification(*, user, review_id, author_notified_at):
    from core.forms import AuthorNotificationForm

    require_superuser(user)
    review = _lock_review(review_id)
    _require_current_review(review)

    if review.status not in (Review.Status.ACCEPTED, Review.Status.REJECTED):
        raise ValidationError(
            "Powiadomienie można oznaczyć po przyjęciu albo odrzuceniu tekstu."
        )

    if review.decision_at and review.decision_at > timezone.localdate():
        raise ValidationError("Nie nadszedł jeszcze dzień zaplanowanej decyzji.")

    if review.copied_text_id is not None:
        raise ValidationError(
            "Tekst został już przeniesiony do procesu wydawniczego."
        )

    cleaned = _validate_form(
        AuthorNotificationForm(
            {
                "author_notified": author_notified_at is not None,
                "author_notified_at": author_notified_at or "",
            }
        )
    )

    review.author_notified_at = cleaned["author_notified_at"]
    review.full_clean()
    review.save(update_fields=["author_notified_at"])
    return review


def _resolve_copy_author(review, *, contract_received):
    if review.author_id is not None:
        author = get_object_or_404(
            Author.objects.select_for_update(),
            pk=review.author_id,
        )
    else:
        email = normalize_email(review.email)
        if not email:
            raise ValidationError(
                "Przed przeniesieniem uzupełnij adres e-mail autora."
            )

        matches = list(
            Author.objects.select_for_update()
            .filter(email__iexact=email)
            .order_by("pk")
        )
        if len(matches) > 1:
            raise ValidationError(
                "Adres e-mail pasuje do kilku autorów. "
                "Najpierw uporządkuj dane i powiąż zgłoszenie z autorem."
            )

        author = matches[0] if matches else None

        if author is not None:
            expected_name = normalize_author_name(
                f"{review.author_first_name} {review.author_last_name}"
            )
            actual_name = normalize_author_name(
                f"{author.first_name} {author.last_name}"
            )
            if expected_name != actual_name:
                raise ValidationError(
                    "Adres e-mail pasuje do autora o innych danych. "
                    "Zweryfikuj powiązanie w panelu administracyjnym."
                )
        else:
            if not contract_received:
                raise ValidationError(
                    "Przed przeniesieniem potwierdź otrzymanie umowy autora."
                )

            author = Author(
                first_name=review.author_first_name,
                last_name=review.author_last_name,
                email=email,
                has_contract=True,
            )
            author.full_clean()
            author.save()
            return author

    if not author.has_contract:
        if not contract_received:
            raise ValidationError(
                "Autor nie ma potwierdzonej umowy. "
                "Potwierdź jej otrzymanie przed przeniesieniem tekstu."
            )

        author.has_contract = True
        author.full_clean()
        author.save(update_fields=["has_contract"])

    return author


@transaction.atomic
def copy_review_to_text(*, user, review_id, contract_received=False):
    require_superuser(user)

    if type(contract_received) is not bool:
        raise ValidationError("Nieprawidłowe potwierdzenie otrzymania umowy.")

    review = _lock_review(review_id)
    _require_current_review(review)

    if review.copied_text_id is not None:
        return get_object_or_404(Text, pk=review.copied_text_id)

    if review.status != Review.Status.ACCEPTED:
        raise ValidationError(
            "Do procesu wydawniczego można przenieść wyłącznie przyjęty tekst."
        )

    if review.author_notified_at is None:
        raise ValidationError("Najpierw oznacz powiadomienie autora.")

    author = _resolve_copy_author(
        review,
        contract_received=contract_received,
    )

    text = Text(
        title=review.title,
        anthology_id=review.anthology_id,
        length=review.length,
        content_warnings=review.content_warnings,
        current_workflow_cycle=1,
    )
    text.full_clean()
    text.save()
    text.authors.add(author)

    stage = WorkflowStage(
        text=text,
        workflow_cycle=text.current_workflow_cycle,
        stage_type=WorkflowStage.StageType.READY_FOR_EDITING,
        iteration=1,
        started_at=None,
        ended_at=None,
        is_completed=False,
    )
    stage.full_clean()
    stage.save()

    review.author = author
    review.copied_text = text
    review.full_clean()
    review.save(update_fields=["author", "copied_text"])

    return text


def _refresh_import_form(original, checked):
    """
    Przekazuje do widoku nowy token i aktualne ostrzeżenia.

    Widok dodaje komunikat walidacji do oryginalnego formularza,
    zachowując wprowadzone rekordy.
    """
    original.data = checked.data.copy()
    original.import_warnings = getattr(checked, "import_warnings", [])
    original.parsed_records = getattr(checked, "parsed_records", [])

    if hasattr(checked, "warning_payload"):
        original.warning_payload = checked.warning_payload

    # BoundField może przechowywać wcześniej odczytaną wartość tokenu.
    original._bound_fields_cache.clear()


@transaction.atomic
def import_reviews(*, user, form):
    from core.forms import ReviewBulkImportForm

    require_superuser(user)

    if not isinstance(form, ReviewBulkImportForm) or not form.is_bound:
        raise ValidationError("Import wymaga wypełnionego formularza.")

    # Importy wykonywane przez ten serwis są serializowane także wtedy,
    # gdy dotyczą różnych naborów. Duplikaty wykrywamy między naborami.
    # Blokujemy istniejące antologie zawsze w tej samej kolejności.
    anthology_ids = list(
        Anthology.objects.select_for_update()
        .order_by("pk")
        .values_list("pk", flat=True)
    )

    if not anthology_ids:
        raise ValidationError("Najpierw utwórz nabór do antologii.")

    # Nie ufamy cached cleaned_data ani parsed_records przekazanego
    # formularza. Ponownie przetwarzamy oryginalne dane, podpis,
    # użytkownika, ostrzeżenia i ich potwierdzenie.
    checked = ReviewBulkImportForm(form.data.copy(), user=user)

    if not checked.is_valid():
        _refresh_import_form(form, checked)
        raise ValidationError(
            [
                str(error)
                for errors in checked.errors.values()
                for error in errors
            ]
        )

    anthology = checked.cleaned_data["anthology"]
    if anthology.pk not in anthology_ids:
        raise ValidationError("Wybrany nabór nie jest już dostępny.")

    author_ids = {
        record["author_id"]
        for record in checked.parsed_records
        if record.get("author_id") is not None
    }
    authors = {
        author.pk: author
        for author in (
            Author.objects.select_for_update()
            .filter(pk__in=author_ids)
            .order_by("pk")
        )
    }

    if set(authors) != author_ids:
        raise ValidationError(
            "Dane autorów zmieniły się podczas importu. "
            "Sprawdź formularz i ponów operację."
        )

    # Po uzyskaniu blokad autorów ponownie sprawdzamy m.in. czarną listę.
    # Zmiana ostrzeżeń unieważnia wcześniejsze potwierdzenie.
    checked = ReviewBulkImportForm(form.data.copy(), user=user)
    if not checked.is_valid():
        _refresh_import_form(form, checked)
        raise ValidationError(
            [
                str(error)
                for errors in checked.errors.values()
                for error in errors
            ]
        )

    final_author_ids = {
        record["author_id"]
        for record in checked.parsed_records
        if record.get("author_id") is not None
    }
    if final_author_ids != author_ids:
        raise ValidationError(
            "Powiązania autorów zmieniły się podczas importu. "
            "Sprawdź dane i ponów operację."
        )

    pending_reviews = []

    for record in checked.parsed_records:
        author = authors.get(record.get("author_id"))
        review = Review(
            author=author,
            author_first_name=(
                author.first_name if author else record["author_first_name"]
            ),
            author_last_name=(
                author.last_name if author else record["author_last_name"]
            ),
            email=author.email if author else normalize_email(record["email"]),
            phone_number=record["phone_number"],
            title=record["title"],
            genre=record["genre"],
            length=record["length"],
            content_warnings=record["content_warnings"],
            anthology=checked.cleaned_data["anthology"],
            status=Review.Status.NEW,
            old_reviews=False,
        )

        try:
            review.full_clean()
        except ValidationError as error:
            raise ValidationError(
                f'Wiersz {record["line_number"]}: '
                + " ".join(error.messages)
            ) from error

        from texts.blacklist import apply_blacklist
        apply_blacklist(review)
        pending_reviews.append(review)

    if not pending_reviews:
        raise ValidationError("Brak zgłoszeń do zaimportowania.")

    for review in pending_reviews:
        review.save(force_insert=True)
    return len(pending_reviews)
