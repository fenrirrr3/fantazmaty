from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, router
from django.utils import timezone

from authors.models import Author
from people.models import Person
from core.normalization import NormalizedModelMixin, REVIEW_FIELDS, TEXT_FIELDS


MAX_REVIEWERS = 6


class ReviewOpinion(models.TextChoices):
    READING = "reading", "W CZYTANIU"
    YES = "yes", "TAK"
    YES_MAYBE = "yes_maybe", "TAK/MOŻE"
    MAYBE = "maybe", "MOŻE"
    MAYBE_NO = "maybe_no", "MOŻE/NIE"
    NO = "no", "NIE"


class Anthology(models.Model):
    class Status(models.TextChoices):
        PUBLISHED = "published", "Wydane"
        UNPUBLISHED = "unpublished", "Niewydane"
        IN_PREPARATION = "in_preparation", "W przygotowaniu"

    class PrintStatus(models.TextChoices):
        NO = "no", "Nie"
        PLANNED = "planned", "Planowane"
        YES = "yes", "Tak"

    class CoverStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Nierozpoczęta"
        IN_PROGRESS = "in_progress", "W przygotowaniu"
        READY = "ready", "Gotowa"

    title = models.CharField(
        "tytuł antologii",
        max_length=255,
    )

    status = models.CharField(
        "status",
        max_length=20,
        choices=Status.choices,
        default=Status.UNPUBLISHED,
    )

    has_illustrations = models.BooleanField(
        "ilustracje",
        default=False,
    )

    print_status = models.CharField(
        "wydanie drukowane",
        max_length=20,
        choices=PrintStatus.choices,
        default=PrintStatus.NO,
    )

    cover_status = models.CharField(
        "stan okładki",
        max_length=20,
        choices=CoverStatus.choices,
        default=CoverStatus.NOT_STARTED,
    )

    cover_author = models.CharField(
        "autor okładki",
        max_length=255,
        blank=True,
    )

    cover_notes = models.TextField(
        "informacje o okładce",
        blank=True,
    )

    class Meta:
        verbose_name = "antologia"
        verbose_name_plural = "antologie"
        ordering = ("title", "pk")

    def __str__(self):
        return self.title

    @property
    def typesetting_task(self):
        # Korzysta z prefetch_related("production_tasks"), jeśli wykonano.
        return next(
            (
                task
                for task in self.production_tasks.all()
                if task.task_type == AnthologyTask.TaskType.TYPESETTING
            ),
            None,
        )


class AnthologyTask(models.Model):
    class TaskType(models.TextChoices):
        TYPESETTING = "typesetting", "Skład"
        BLURB = "blurb", "Blurb"
        BANNERS = "banners", "Bannery"

    class Status(models.TextChoices):
        NOT_COMMISSIONED = "not_commissioned", "Niezlecone"
        COMMISSIONED = "commissioned", "Zlecone"
        READY = "ready", "Gotowe"

    anthology = models.ForeignKey(
        Anthology,
        on_delete=models.CASCADE,
        related_name="production_tasks",
        verbose_name="antologia",
    )

    task_type = models.CharField(
        "rodzaj zadania",
        max_length=20,
        choices=TaskType.choices,
    )

    assigned_to = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="anthology_tasks",
        verbose_name="przypisana osoba",
        null=True,
        blank=True,
    )

    status = models.CharField(
        "status",
        max_length=20,
        choices=Status.choices,
        default=Status.NOT_COMMISSIONED,
        db_index=True,
    )

    commissioned_at = models.DateField(
        "data zlecenia",
        null=True,
        blank=True,
        editable=False,
    )

    class Meta:
        verbose_name = "zadanie antologii"
        verbose_name_plural = "zadania antologii"
        ordering = (
            "anthology__title",
            "task_type",
            "pk",
        )
        constraints = [
            models.UniqueConstraint(
                fields=("anthology", "task_type"),
                name="unique_task_type_per_anthology",
            ),
        ]

    def __str__(self):
        return f"{self.anthology.title} – {self.get_task_type_display()}"

    def clean(self):
        super().clean()

        errors = {}

        if (
            self.status != self.Status.NOT_COMMISSIONED
            and not self.assigned_to_id
        ):
            errors["assigned_to"] = (
                "Status „Zlecone” lub „Gotowe” wymaga "
                "wybrania osoby odpowiedzialnej."
            )

        if (
            self.status == self.Status.NOT_COMMISSIONED
            and self.assigned_to_id
        ):
            errors["status"] = (
                "Jeżeli wybrano osobę odpowiedzialną, "
                "zmień status na „Zlecone”."
            )

        if errors:
            raise ValidationError(errors)

    def save(
        self,
        *,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):
        using = using or router.db_for_write(type(self), instance=self)

        if update_fields is not None:
            update_fields = set(update_fields)

            if not update_fields:
                return

        relevant_fields = {"status", "assigned_to", "assigned_to_id"}
        update_commission_date = (
            update_fields is None
            or bool(relevant_fields.intersection(update_fields))
        )

        if update_commission_date:
            previous = None

            if self.pk is not None and not self._state.adding:
                previous = (
                    type(self).objects.using(using)
                    .filter(pk=self.pk)
                    .values("status", "assigned_to_id")
                    .first()
                )

            effective_status = self.status
            effective_assigned_to_id = self.assigned_to_id

            # Przy częściowym zapisie uwzględniaj wyłącznie pola,
            # które rzeczywiście zostaną zapisane.
            if previous is not None and update_fields is not None:
                if "status" not in update_fields:
                    effective_status = previous["status"]

                if not {"assigned_to", "assigned_to_id"}.intersection(
                    update_fields
                ):
                    effective_assigned_to_id = previous["assigned_to_id"]

            newly_commissioned = (
                effective_status == self.Status.COMMISSIONED
                and (
                    previous is None
                    or previous["status"] != self.Status.COMMISSIONED
                    or previous["assigned_to_id"]
                    != effective_assigned_to_id
                )
            )

            date_changed = False

            if newly_commissioned:
                self.commissioned_at = timezone.localdate()
                date_changed = True
            elif effective_status == self.Status.NOT_COMMISSIONED:
                self.commissioned_at = None
                date_changed = True

            if date_changed and update_fields is not None:
                update_fields.add("commissioned_at")

        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


class Text(NormalizedModelMixin, models.Model):
    historical_source = models.CharField("źródło importu historycznego", max_length=100, blank=True, default="", editable=False)
    historical_source_row = models.PositiveIntegerField("LP importu historycznego", null=True, blank=True, editable=False)
    normalization_fields = TEXT_FIELDS
    is_historical = models.BooleanField("tekst historyczny", default=False, editable=False)
    title = models.CharField(
        "tytuł",
        max_length=255,
    )

    authors = models.ManyToManyField(
        Author,
        related_name="texts",
        verbose_name="autorzy",
    )

    anthology = models.ForeignKey(
        Anthology,
        on_delete=models.PROTECT,
        related_name="texts",
        verbose_name="antologia",
        null=True,
        blank=True,
    )

    length = models.PositiveIntegerField(
        "długość",
        validators=[MinValueValidator(1)],
    )

    content_warnings = models.TextField(
        "trigger warningi",
        blank=True,
    )

    coordinator_note = models.TextField(
        "notatka koordynatora",
        blank=True,
    )

    current_workflow_cycle = models.PositiveIntegerField(
        "aktualny przebieg workflow",
        default=1,
        editable=False,
        validators=[MinValueValidator(1)],
    )

    class Meta:
        verbose_name = "tekst"
        verbose_name_plural = "teksty"
        constraints = [models.UniqueConstraint(fields=("historical_source", "historical_source_row"), name="unique_text_historical_source")]

        ordering = ("title", "pk")

    def __str__(self):
        return self.title

    @property
    def authors_display(self):
        # Dostęp do danych autorów kontrolują widoki i uprawnienia.
        return ", ".join(str(author) for author in self.authors.all())

    @property
    def author_emails(self):
        return ", ".join(
            author.email
            for author in self.authors.all()
            if author.email
        )


class TextNote(models.Model):
    text = models.ForeignKey(
        Text,
        on_delete=models.CASCADE,
        related_name="notes",
        verbose_name="tekst",
    )

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="text_notes",
        verbose_name="autor notatki",
        null=True,
        blank=True,
    )

    content = models.TextField(
        "treść notatki",
    )

    is_important = models.BooleanField(
        "ważne",
        default=False,
    )

    created_at = models.DateTimeField(
        "data dodania",
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        verbose_name = "notatka do tekstu"
        verbose_name_plural = "notatki do tekstów"
        ordering = ("-created_at", "-pk")

    def __str__(self):
        author_name = "nieznany autor"

        if self.author_id is not None:
            author_name = (
                self.author.get_full_name()
                or self.author.get_username()
            )

        if self.created_at is None:
            formatted_date = "bez daty"
        else:
            created_at = self.created_at

            if timezone.is_aware(created_at):
                created_at = timezone.localtime(created_at)

            formatted_date = created_at.strftime("%d.%m.%Y %H:%M")

        return f"{self.text.title} – {author_name} – {formatted_date}"


class ReviewQuerySet(models.QuerySet):
    def visible_to(self, user):
        return self if user.is_active and user.is_superuser else self.filter(is_hidden=False)

    def awaiting_notification(self):
        return self.filter(
            models.Q(decision_at__lte=timezone.localdate()) | models.Q(decision_at__isnull=True),
            old_reviews=False, status__in=("accepted", "rejected"),
            author_notified_at__isnull=True,
        )

    def current(self):
        return self.filter(old_reviews=False)

    def archived(self):
        return self.filter(old_reviews=True)

    def for_statistics(self):
        return self.current().filter(is_hidden=False)


class Review(NormalizedModelMixin, models.Model):
    normalization_fields = REVIEW_FIELDS
    is_hidden = models.BooleanField("ukryty", default=False, db_index=True)

    @property
    def display_status(self):
        if self.is_hidden and self.decision_at and self.decision_at > timezone.localdate():
            return f"Ukryty – odrzucenie zaplanowane na {self.decision_at:%d.%m.%Y}"
        return self.get_status_display()
    class Status(models.TextChoices):
        NEW = "new", "Nowy"
        IN_REVIEW = "in_review", "W trakcie oceny"
        ACCEPTED = "accepted", "Przyjęty"
        REJECTED = "rejected", "Odrzucony"
        WITHDRAWN = "withdrawn", "Wycofany"

    author = models.ForeignKey(
        Author,
        on_delete=models.PROTECT,
        related_name="review_submissions",
        verbose_name="autor w bazie",
        null=True,
        blank=True,
    )

    # Dane zgłoszenia mogą istnieć przed powiązaniem z rekordem Author.
    # Wyłącznie superuser może je odczytywać w interfejsie.
    author_first_name = models.CharField(
        "imię autora",
        max_length=100,
    )

    author_last_name = models.CharField(
        "nazwisko autora",
        max_length=100,
    )

    title = models.CharField(
        "tytuł",
        max_length=255,
    )

    genre = models.CharField(
        "gatunek",
        max_length=100,
    )

    length = models.PositiveIntegerField(
        "długość",
        validators=[MinValueValidator(1)],
    )

    content_warnings = models.TextField(
        "trigger warningi",
        blank=True,
    )

    email = models.EmailField(
        "adres e-mail",
    )

    phone_number = models.CharField(
        "numer telefonu",
        max_length=30,
        blank=True,
    )

    anthology = models.ForeignKey(
        Anthology,
        on_delete=models.PROTECT,
        related_name="reviews",
        verbose_name="nabór",
    )

    old_reviews = models.BooleanField(
        "stare recenzje",
        default=False,
        db_index=True,
        help_text=(
            "Recenzja archiwalna, wyłączona ze statystyk "
            "i bieżącego przydzielania pracy."
        ),
    )

    created_at = models.DateField(
        "data wpisania",
        auto_now_add=True,
    )

    status = models.CharField(
        "status",
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )

    decision_at = models.DateField(
        "data decyzji",
        null=True,
        blank=True,
    )

    author_notified_at = models.DateField(
        "data powiadomienia autora",
        null=True,
        blank=True,
    )

    copied_text = models.OneToOneField(
        Text,
        on_delete=models.SET_NULL,
        related_name="source_review",
        verbose_name="tekst utworzony z recenzji",
        null=True,
        blank=True,
    )

    # Domyślny manager zachowuje archiwum m.in. do wykrywania duplikatów.
    # Statystyki muszą korzystać z for_statistics().
    objects = ReviewQuerySet.as_manager()

    class Meta:
        verbose_name = "recenzja"
        verbose_name_plural = "recenzje"
        ordering = ("-created_at", "-pk")

    def __str__(self):
        # Bez tożsamości autora w etykietach relacji, logach i adminie.
        return self.title

    @property
    def is_copied_to_text(self):
        return self.copied_text_id is not None

    @property
    def author_was_notified(self):
        return self.author_notified_at is not None


class Reviewers(models.Model):
    """Uwagi ogólne do ocen; przydziały przechowuje ReviewAssignment."""

    Opinion = ReviewOpinion

    review = models.OneToOneField(
        Review,
        on_delete=models.CASCADE,
        related_name="reviewers",
        verbose_name="recenzja",
    )

    general_notes = models.TextField(
        "uwagi ogólne",
        blank=True,
    )

    class Meta:
        verbose_name = "oceny recenzentów"
        verbose_name_plural = "oceny recenzentów"
        ordering = ("review__title", "pk")

    def __str__(self):
        return f"Oceny: {self.review.title}"

    @property
    def has_free_slot(self):
        if self.review_id is None:
            return False

        review = self.review

        if review.old_reviews:
            return False

        # Usunięcie konta nie zwalnia historycznego miejsca z oceną.
        # Zwolnienie przydziału obsługuje serwis recenzji.
        return review.assignments.count() < MAX_REVIEWERS


class ReviewAssignmentQuerySet(models.QuerySet):
    def current(self):
        return self.filter(review__old_reviews=False)

    def for_statistics(self):
        return self.current().filter(review__is_hidden=False)

    def for_user(self, user):
        if not user.is_authenticated or user.pk is None:
            return self.none()

        return self.filter(user_id=user.pk)


class ReviewAssignment(models.Model):
    """Jeden rekord odpowiada jednemu miejscu recenzenta w zgłoszeniu."""

    MAX_REVIEWERS = MAX_REVIEWERS
    Opinion = ReviewOpinion

    review = models.ForeignKey(
        Review,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name="recenzja",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="review_assignments",
        verbose_name="recenzent",
        null=True,
        blank=True,
    )

    position = models.PositiveSmallIntegerField(
        "numer miejsca",
        validators=[
            MinValueValidator(1),
            MaxValueValidator(MAX_REVIEWERS),
        ],
    )

    opinion = models.CharField(
        "opinia",
        max_length=10,
        choices=Opinion.choices,
        default=Opinion.READING,
    )

    notes = models.TextField(
        "uwagi recenzenta",
        blank=True,
    )

    assigned_at = models.DateTimeField(
        "data przydzielenia",
        auto_now_add=True,
    )

    opinion_changed_at = models.DateField(
        "data zmiany opinii",
        default=timezone.localdate,
        editable=False,
    )

    objects = ReviewAssignmentQuerySet.as_manager()

    class Meta:
        verbose_name = "przydział recenzenta"
        verbose_name_plural = "przydziały recenzentów"
        ordering = ("position", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("review", "user"),
                name="unique_reviewer_per_review",
            ),
            models.UniqueConstraint(
                fields=("review", "position"),
                name="unique_reviewer_position",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    position__gte=1,
                    position__lte=MAX_REVIEWERS,
                ),
                name="review_position_valid_range",
            ),
            models.CheckConstraint(
                condition=models.Q(opinion__in=ReviewOpinion.values),
                name="review_opinion_valid",
            ),
        ]

    def __str__(self):
        return (
            f"{self.review.title} – recenzent {self.position} "
            f"– {self.get_opinion_display()}"
        )

    def save(
        self,
        *,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):
        using = using or router.db_for_write(type(self), instance=self)

        if update_fields is not None:
            update_fields = set(update_fields)

            if not update_fields:
                return

        opinion_is_saved = (
            update_fields is None or "opinion" in update_fields
        )

        if (
            opinion_is_saved
            and self.pk is not None
            and not self._state.adding
        ):
            previous = (
                type(self).objects.using(using)
                .filter(pk=self.pk)
                .values("opinion")
                .first()
            )

            if previous is not None and previous["opinion"] != self.opinion:
                self.opinion_changed_at = timezone.localdate()

                if update_fields is not None:
                    update_fields.add("opinion_changed_at")

        # Przydzielanie, zwalnianie miejsc i zapis ocen muszą odbywać się
        # przez serwis z kontrolą uprawnień oraz blokadą rekordu recenzji.
        # QuerySet.update() i bulk_update() omijają tę metodę.
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


class Extract(NormalizedModelMixin, models.Model):
    """Jeden autor w jednym naborze; tytuły pozostają listami w rekordzie."""
    normalization_fields = {"email": REVIEW_FIELDS["email"], "phone_number": REVIEW_FIELDS["phone_number"]}
    author = models.ForeignKey(Author, on_delete=models.PROTECT, related_name='extracts', verbose_name='autor')
    full_name = models.CharField('imię i nazwisko', max_length=255)
    email = models.EmailField('adres e-mail')
    phone_number = models.CharField('numer telefonu', max_length=50, blank=True)
    title = models.TextField('nadesłane tytuły', help_text='Jeden tytuł w wierszu lub tytuły rozdzielone średnikami.')
    submitted_at = models.DateField('pierwsza data nadesłania', default=timezone.localdate)
    submission_dates = models.TextField('daty nadesłania', blank=True, help_text='Daty RRRR-MM-DD, osobno w wierszach lub rozdzielone średnikami.')
    recruitment = models.CharField('antologia / nabór', max_length=255)
    accepted_titles = models.TextField('przyjęte teksty', blank=True)
    rejected_titles = models.TextField('odrzucone teksty', blank=True)

    class Status(models.TextChoices):
        NEW = 'new', 'Bez decyzji'
        ACCEPTED = 'accepted', 'Tylko przyjęte'
        REJECTED = 'rejected', 'Tylko odrzucone'
        MIXED = 'mixed', 'Przyjęte i odrzucone'

    status = models.CharField('rodzaj decyzji', max_length=20, choices=Status.choices, default=Status.NEW, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'udział autora w Ekstraktach'
        verbose_name_plural = 'ekstrakty'
        ordering = ('recruitment', 'full_name', 'pk')
        constraints = [models.UniqueConstraint(fields=('author', 'recruitment'), name='unique_extract_author_recruitment')]

    def prepare_lists(self):
        from .extract_data import split_list, normalize_key, parse_dates
        from django.core.exceptions import ValidationError
        self.recruitment = ' '.join(self.recruitment.split())
        for field in ('title', 'accepted_titles', 'rejected_titles'):
            setattr(self, field, '\n'.join(split_list(getattr(self, field))))
        submitted = {normalize_key(v) for v in split_list(self.title)}
        accepted = {normalize_key(v) for v in split_list(self.accepted_titles)}
        rejected = {normalize_key(v) for v in split_list(self.rejected_titles)}
        errors = {}
        if accepted & rejected:
            errors['rejected_titles'] = 'Ten sam tytuł nie może być przyjęty i odrzucony.'
        if accepted - submitted:
            errors['accepted_titles'] = 'Przyjęte tytuły muszą występować na liście nadesłanych.'
        if rejected - submitted:
            errors['rejected_titles'] = 'Odrzucone tytuły muszą występować na liście nadesłanych.'
        try:
            dates = parse_dates(self.submission_dates or self.submitted_at.isoformat())
            self.submission_dates = '\n'.join(dates)
            from datetime import date
            self.submitted_at = date.fromisoformat(dates[0])
        except (ValueError, AttributeError) as error:
            errors['submission_dates'] = str(error)
        self.status = self.Status.MIXED if accepted and rejected else self.Status.ACCEPTED if accepted else self.Status.REJECTED if rejected else self.Status.NEW
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        self.prepare_lists()

    def save(self, *args, **kwargs):
        self.prepare_lists()
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'title', 'accepted_titles', 'rejected_titles', 'submission_dates', 'submitted_at', 'status', 'recruitment'}
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.recruitment} — {self.full_name}'


class HistoricalTextAssignment(models.Model):
    """Imported participation; never an assignment in the active workflow."""
    class Role(models.TextChoices):
        EDITOR = "editor", "Redaktor"
        PROOFREADER = "proofreader", "Korektor"
        VERIFIER = "verifier", "Weryfikator"
        EDITING_COORDINATOR = "editing_coordinator", "Koordynator redakcji"
        VERIFICATION_COORDINATOR = "verification_coordinator", "Koordynator weryfikacji"
        EDITING_VERIFIER = "editing_verifier", "Weryfikacja redakcji"
        FINAL_READER = "final_reader", "Sczytanie"

    text = models.ForeignKey(Text, on_delete=models.CASCADE, related_name="historical_assignments", verbose_name="tekst")
    person = models.ForeignKey(Person, on_delete=models.SET_NULL, null=True, blank=True, related_name="historical_text_assignments", verbose_name="osoba")
    person_name = models.CharField("nazwa osoby ze źródła", max_length=255)
    role = models.CharField("rola", max_length=30, choices=Role.choices)
    position = models.PositiveSmallIntegerField("numer roli", default=1, validators=[MinValueValidator(1)])
    participant = models.PositiveSmallIntegerField("numer uczestnika roli", default=1, validators=[MinValueValidator(1)])
    source_row = models.PositiveIntegerField("LP w tabeli źródłowej", null=True, blank=True, validators=[MinValueValidator(1)])
    source_status = models.CharField("status tekstu w źródle", max_length=12, blank=True, choices=(("ready", "Gotowe"), ("withdrawn", "WYCOFANY")))
    notes = models.TextField("uwagi", blank=True)
    started_at = models.DateField("rozpoczęcie", null=True, blank=True)
    ended_at = models.DateField("zakończenie", null=True, blank=True)
    is_completed = models.BooleanField("potwierdzone wykonanie", default=False)

    class Meta:
        verbose_name = "historyczne przypisanie do tekstu"
        verbose_name_plural = "historyczne przypisania do tekstów"
        ordering = ("text__title", "role", "position", "participant", "pk")
        constraints = [
            models.UniqueConstraint(fields=("text", "role", "position", "participant"), name="unique_history_role_participant"),
            models.CheckConstraint(condition=models.Q(participant__gte=1), name="historical_participant_positive"),
            models.CheckConstraint(condition=models.Q(position__gte=1), name="historical_position_positive"),
            models.CheckConstraint(condition=models.Q(started_at__isnull=True) | models.Q(ended_at__isnull=True) | models.Q(ended_at__gte=models.F("started_at")), name="historical_dates_order"),
        ]

    @property
    def role_label(self):
        label = self.get_role_display()
        if self.position > 1 or self.role in (self.Role.PROOFREADER, self.Role.VERIFIER):
            label += f" {self.position}"
        if self.participant > 1:
            label += f" · osoba {self.participant}"
        return label

    @property
    def display_name(self):
        return str(self.person) if self.person_id else self.person_name

    def clean(self):
        super().clean()
        if self.text_id and not self.text.is_historical:
            raise ValidationError({"text": "Przypisanie historyczne wymaga tekstu historycznego."})
        if self.started_at is not None or self.ended_at is not None:
            raise ValidationError("Historia bez dat: pozostaw rozpoczęcie i zakończenie puste.")
        if self.text_id:
            from workflow.models import WorkflowRoleAssignment
            current_role = None
            if self.role in (self.Role.EDITOR, self.Role.EDITING_COORDINATOR, self.Role.VERIFICATION_COORDINATOR) and self.position == 1:
                current_role = self.role
            elif self.role == self.Role.PROOFREADER and 1 <= self.position <= 4:
                current_role = f"proofreader_{self.position}"
            elif self.role == self.Role.VERIFIER and 1 <= self.position <= 3:
                current_role = f"verifier_{self.position}"
            if self.participant == 1 and current_role and WorkflowRoleAssignment.objects.filter(
                text_id=self.text_id, workflow_cycle=self.text.current_workflow_cycle,
                role=current_role, assigned_to__isnull=False,
            ).exists():
                raise ValidationError("Ta rola ma już podstawowe przypisanie. Historia służy dodatkowym udziałom; nie kopiuj istniejącego przypisania.")

    def __str__(self):
        return f"{self.text}: {self.role_label} – {self.display_name}"
