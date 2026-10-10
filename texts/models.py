from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, MaxLengthValidator
from django.db import models, router, transaction
from django.utils import timezone

from authors.models import Author
from people.models import Person
from core.normalization import NormalizedModelMixin, REVIEW_FIELDS, TEXT_FIELDS
from core.edit_versions import VersionedQuerySet


MAX_REVIEWERS = 6


class ReviewOpinion(models.TextChoices):
    READING = "reading", "W CZYTANIU"
    YES = "yes", "TAK"
    YES_MAYBE = "yes_maybe", "TAK/MOŻE"
    MAYBE = "maybe", "MOŻE"
    MAYBE_NO = "maybe_no", "MOŻE/NIE"
    NO = "no", "NIE"


class Anthology(models.Model):
    objects = VersionedQuerySet.as_manager()
    is_novel = models.BooleanField('powieść', default=False, db_index=True)
    class Status(models.TextChoices):
        IN_PREPARATION = "in_preparation", "W przygotowaniu"
        ABANDONED = "abandoned", "Porzucona"
        READY = "ready", "Gotowa"

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
        default=Status.IN_PREPARATION,
    )

    has_illustrations = models.BooleanField(
        "ilustracje",
        default=False,
    )

    is_translated = models.BooleanField("tłumaczone", default=False, db_index=True,
        help_text="Antologia i jej teksty są widoczne w sekcji Tłumaczenia oraz w panelu admina.")

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

    def _validate_ready_transition(self, using):
        if self.status != self.Status.READY:
            return
        if not self.pk:
            if self.is_novel:
                raise ValidationError({'status': 'Nową powieść dodaj jako „W przygotowaniu”. Gotowość wymaga ukończonych rozdziałów.'})
            return
        previous = type(self).objects.using(using).filter(pk=self.pk).values_list('status', flat=True).first()
        if previous == self.Status.READY:
            return
        if self.is_novel:
            from texts.novels import validate_ready_novel
            validate_ready_novel(self)
        from workflow.models import WorkflowStage
        from django.db.models import Exists, OuterRef
        stages = WorkflowStage.objects.using(using).filter(
            text_id=OuterRef('pk'), workflow_cycle=OuterRef('current_workflow_cycle'), is_current=True,
        )
        unfinished = Text.objects.using(using).filter(anthology_id=self.pk).annotate(
            closed=Exists(stages.filter(stage_type='ready', is_released=True)),
            withdrawn=Exists(stages.filter(stage_type='withdrawn', is_released=True)),
            pending=Exists(stages.exclude(stage_type__in=('ready', 'withdrawn')).filter(is_completed=False)),
        ).filter(withdrawn=False).filter(models.Q(closed=False) | models.Q(pending=True))
        examples = list(unfinished.order_by('pk').values_list('title', 'pk')[:10])
        if examples:
            raise ValidationError({'status': 'Nie można oznaczyć antologii jako gotowej. Niezakończone teksty: '
                + ', '.join(f'{title} (#{pk})' for title, pk in examples)
                + '. Zakończ lub wycofaj ich workflow.'})

    def clean(self):
        super().clean()
        self._validate_novel_kind()
        self._validate_ready_transition(self._state.db or router.db_for_write(type(self), instance=self))
        if self._turns_off_translation(self._state.db or router.db_for_write(type(self), instance=self)):
            from .translations import ordinary_author_links
            ordinary_author_links(self, self._state.db or router.db_for_write(type(self), instance=self))

    def _turns_off_translation(self, using):
        return bool(self.pk and not self.is_translated and type(self).objects.using(using).filter(
            pk=self.pk, is_translated=True,
        ).exists())

    def _validate_novel_kind(self):
        if self.is_novel and self.is_translated:
            raise ValidationError({'is_translated': 'Powieści nie obsługują tłumaczeń. Wyłącz „Tłumaczone”.'})
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values_list('is_novel', flat=True).first()
            if previous != self.is_novel and (self.texts.exists() or self.reviews.exists()):
                raise ValidationError({'is_novel': 'Rodzaj publikacji można zmienić tylko przed dodaniem tekstów, rozdziałów lub zgłoszeń.'})

    def save(self, *args, **kwargs):
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        fields = kwargs.get('update_fields')
        if fields is None or {'is_novel', 'is_translated'} & set(fields):
            self._validate_novel_kind()
        if not self.pk:
            self._validate_ready_transition(using)
        if (fields is None or 'is_translated' in fields) and self._turns_off_translation(using):
            from .translations import ordinary_author_links, restore_ordinary_authors
            with transaction.atomic(using=using):
                ordinary_author_links(self, using)
                self._validate_ready_transition(using)
                result = super().save(*args, **kwargs)
                restore_ordinary_authors(self, using)
                return result
        if self.pk and self.status == self.Status.READY and (fields is None or 'status' in fields):
            with transaction.atomic(using=using):
                type(self).objects.using(using).select_for_update().get(pk=self.pk)
                self._validate_ready_transition(using)
                return super().save(*args, **kwargs)
        with transaction.atomic(using=using):
            return super().save(*args, **kwargs)

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
    objects = VersionedQuerySet.as_manager()
    class TaskType(models.TextChoices):
        TYPESETTING = "typesetting", "Skład"
        BLURB = "blurb", "Blurb"
        BANNERS = "banners", "Bannery"
        COVER_TYPOGRAPHY = "cover_typography", "Typografia okładki"
        AUDIO_DESCRIPTION = "audio_description", "Audiodeskrypcja"

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
    objects = VersionedQuerySet.as_manager()
    chapter_number = models.PositiveIntegerField('numer rozdziału', null=True, blank=True,
                                                  validators=[MinValueValidator(1)])
    for_recording = models.BooleanField("Do nagrywania", default=True, db_index=True)
    audiobook_blacklisted = models.BooleanField("Czarna lista audiobooków", default=False, db_index=True)
    genre = models.CharField("gatunek", max_length=100, blank=True, default="")
    tags = models.TextField("tagi", blank=True, default="", max_length=5000)

    file_url = models.URLField("folder Dropbox", max_length=1000, blank=True)

    import_source = models.CharField("źródło importu", max_length=100, blank=True, default="", editable=False)
    import_source_row = models.PositiveIntegerField("LP importu", null=True, blank=True, editable=False)
    normalization_fields = {**TEXT_FIELDS, "genre": REVIEW_FIELDS["genre"]}
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
        null=True,
        blank=True,
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

    coordinator_note_updated_at = models.DateTimeField("data zapisania notatki koordynatora", null=True, blank=True, editable=False)

    current_workflow_cycle = models.PositiveIntegerField(
        "aktualny przebieg workflow",
        default=1,
        editable=False,
        validators=[MinValueValidator(1)],
    )

    class Meta:
        verbose_name = "tekst"
        verbose_name_plural = "teksty"
        constraints = [models.UniqueConstraint(fields=("import_source", "import_source_row"), name="unique_text_import_source"),
                       models.UniqueConstraint(fields=('anthology', 'chapter_number'), name='unique_novel_chapter_number'),
                       models.CheckConstraint(condition=models.Q(chapter_number__isnull=False) | models.Q(length__isnull=False),
                                              name='ordinary_text_requires_length')]

        ordering = ("title", "pk")

    def __str__(self):
        return self.title

    def _validate_anthology_move(self, using):
        if not self.anthology_id:
            return
        previous = type(self).objects.using(using).filter(pk=self.pk).values_list('anthology_id', flat=True).first() if self.pk else None
        if self.pk and previous == self.anthology_id:
            return
        if not Anthology.objects.using(using).filter(pk=self.anthology_id, status=Anthology.Status.READY).exists():
            return
        from workflow.models import WorkflowStage
        stages = WorkflowStage.objects.using(using).filter(text_id=self.pk,
            workflow_cycle=self.current_workflow_cycle, is_current=True) if self.pk else WorkflowStage.objects.none()
        withdrawn = stages.filter(stage_type='withdrawn', is_released=True).exists()
        finished = stages.filter(stage_type='ready', is_released=True).exists()
        pending = stages.exclude(stage_type__in=('ready', 'withdrawn')).filter(is_completed=False).exists()
        if not withdrawn and (not finished or pending):
            raise ValidationError({'anthology': 'Nie można przenieść niedokończonego tekstu do gotowej antologii. Najpierw zakończ jego workflow albo wybierz antologię w przygotowaniu.'})

    def clean(self):
        super().clean()
        self._validate_anthology_move(self._state.db or router.db_for_write(type(self), instance=self))
        self._validate_chapter()

    def clean_fields(self, exclude=None):
        if self.chapter_number and self.anthology_id and self.anthology.is_novel:
            self.title = f'Rozdział {self.chapter_number}'
        super().clean_fields(exclude=exclude)
        if self.length is None and not self.chapter_number and 'length' not in (exclude or ()):
            raise ValidationError({'length': 'Podaj długość tekstu.'})

    def _validate_chapter(self):
        novel = bool(self.anthology_id and self.anthology.is_novel)
        if novel and not self.chapter_number:
            raise ValidationError({'chapter_number': 'Rozdział powieści wymaga numeru.'})
        if not novel and self.chapter_number is not None:
            raise ValidationError({'chapter_number': 'Numer rozdziału dotyczy wyłącznie powieści.'})

    def save(self, *args, **kwargs):
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        fields = kwargs.get('update_fields')
        if self.chapter_number and self.anthology_id and self.anthology.is_novel:
            self.title = f'Rozdział {self.chapter_number}'
            if fields is not None and {'title', 'chapter_number'} & set(fields):
                fields = set(fields) | {'title'}
                kwargs['update_fields'] = fields
        if fields is None or {'anthology', 'anthology_id', 'chapter_number'} & set(fields):
            self._validate_chapter()
        if self.audiobook_blacklisted and (fields is None or {'for_recording', 'audiobook_blacklisted'} & set(fields)):
            self.for_recording = False
            if fields is not None:
                fields = set(fields) | {'for_recording'}
                kwargs['update_fields'] = fields
        if fields is None or 'coordinator_note' in fields:
            previous_note = type(self).objects.using(using).filter(pk=self.pk).values_list('coordinator_note', flat=True).first() if self.pk else ''
            if previous_note != self.coordinator_note:
                self.coordinator_note_updated_at = timezone.now() if self.coordinator_note.strip() else None
                if fields is not None:
                    fields = set(fields) | {'coordinator_note_updated_at'}
                    kwargs['update_fields'] = fields
        if self.pk and (fields is None or 'anthology' in fields or 'anthology_id' in fields):
            with transaction.atomic(using=using):
                previous = type(self).objects.using(using).select_for_update().filter(pk=self.pk).values_list('anthology_id', flat=True).first()
                if previous != self.anthology_id and self.anthology_id:
                    Anthology.objects.using(using).select_for_update().get(pk=self.anthology_id)
                    self._validate_anthology_move(using)
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)

    @property
    def authors_display(self):
        # Dostęp do danych autorów kontrolują widoki i uprawnienia.
        return ", ".join(author.display_name for author in self.authors.all())

    @property
    def author_emails(self):
        return ", ".join(
            author.email
            for author in self.authors.all()
            if author.email
        )


class TranslationPerson(models.Model):
    objects = VersionedQuerySet.as_manager()
    first_name = models.CharField('imię', max_length=100)
    last_name = models.CharField('nazwisko', max_length=100)
    pseudonym = models.CharField('pseudonim', max_length=100, blank=True)
    email = models.EmailField('adres e-mail', blank=True)
    phone_number = models.CharField('numer telefonu', max_length=50, blank=True)
    notes = models.TextField('notatki', blank=True)
    legacy_author_id = models.PositiveBigIntegerField(null=True, unique=True, editable=False)

    class Meta:
        abstract = True
        ordering = ('last_name', 'first_name', 'pk')

    @property
    def display_name(self):
        """Podpis poza panelem admina; nie zmienia danych osobowych."""
        return self.pseudonym.strip() or str(self)

    def __str__(self):
        return f'{self.first_name} {self.last_name}'.strip()


class ForeignAuthor(TranslationPerson):
    class Meta(TranslationPerson.Meta):
        abstract = False
        verbose_name = 'autor zagraniczny'
        verbose_name_plural = 'autorzy zagraniczni'


class Translator(TranslationPerson):
    language = models.CharField('język / języki pracy', max_length=255, blank=True,
                                help_text='Np. angielski, niemiecki.')

    class Meta(TranslationPerson.Meta):
        abstract = False
        verbose_name = 'tłumacz'
        verbose_name_plural = 'tłumacze'


class TextTranslation(models.Model):
    objects = VersionedQuerySet.as_manager()
    original_verifier = models.CharField("Weryfikacja z oryginałem", max_length=255, blank=True, help_text="Imię i nazwisko osoby weryfikującej przekład z oryginałem.")
    text = models.OneToOneField(Text, on_delete=models.CASCADE, related_name='translation', verbose_name='tekst')
    foreign_authors = models.ManyToManyField(ForeignAuthor, blank=True, related_name='translations', verbose_name='autor zagraniczny')
    translators = models.ManyToManyField(Translator, blank=True, related_name='translations', verbose_name='tłumacz')

    class Meta:
        verbose_name = 'tłumaczenie'
        verbose_name_plural = 'tłumaczenia'
        ordering = ('text__anthology__title', 'text__title', 'pk')

    def __str__(self):
        return self.text.title


class TextNote(models.Model):
    objects = VersionedQuerySet.as_manager()
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


class ReviewQuerySet(VersionedQuerySet):
    def visible_to(self, user):
        return self if user.is_active and user.is_superuser else self.filter(is_hidden=False)

    def accessible_to(self, user):
        """Object visibility shared by detail views and edit-conflict responses."""
        from core.permissions import can_view_review_archive

        reviews = self.visible_to(user)
        return reviews if can_view_review_archive(user) else reviews.filter(old_reviews=False)

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
        return self.filter(models.Q(old_reviews=True) | models.Q(is_hidden=False))


class Review(NormalizedModelMixin, models.Model):
    publication_detached = models.BooleanField("Świadomie odłączono od tekstu", default=False,
        help_text="Nie pokazuj jako nowego tekstu do przeniesienia. Odznacz wyłącznie, jeśli świadomie chcesz ponownie utworzyć tekst.")

    def save(self, *args, **kwargs):
        fields = kwargs.get('update_fields')
        if self.pk and (fields is None or 'copied_text' in fields or 'copied_text_id' in fields):
            previous = type(self).objects.filter(pk=self.pk).values_list('copied_text_id', flat=True).first()
            if previous and not self.copied_text_id:
                self.publication_detached = True
            elif self.copied_text_id:
                self.publication_detached = False
            if fields is not None:
                kwargs['update_fields'] = set(fields) | {'publication_detached'}
        return super().save(*args, **kwargs)

    author_pseudonym = models.CharField("pseudonim autora", max_length=100, blank=True, default="")

    author_message = models.TextField(
        "wiadomość od autora – tylko superuser", blank=True, default="",
        validators=[MaxLengthValidator(20000)],
    )

    normalization_fields = REVIEW_FIELDS
    is_hidden = models.BooleanField("ukryty", default=False, db_index=True)

    @property
    def display_status(self):
        if self.is_hidden and self.decision_at and self.decision_at > timezone.localdate():
            return f"Ukryty – odrzucenie zaplanowane na {self.decision_at:%d.%m.%Y}"
        return self.get_status_display()
    class Status(models.TextChoices):
        NEW = "new", "Do recenzji"
        IN_REVIEW = "in_review", "W recenzjach"
        TO_DECIDE = "to_decide", "Do decyzji"
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

    coauthors = models.ManyToManyField(
        Author, blank=True, related_name="coauthored_review_submissions",
        verbose_name="współautorzy", help_text="Dodatkowi autorzy, poza autorem głównym.",
    )

    file_url = models.URLField("folder Dropbox", max_length=1000, blank=True)

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
        blank=True,
    )

    length = models.PositiveIntegerField(
        "długość",
        null=True, blank=True,
        validators=[MinValueValidator(1)],
    )

    content_warnings = models.TextField(
        "trigger warningi",
        blank=True,
    )

    email = models.EmailField(
        "adres e-mail",
        blank=True,
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
            "Oddane opinie archiwalne są liczone do dorobku, "
            "bez wpływu na bieżące obciążenie i przydzielanie pracy."
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
        constraints = [models.CheckConstraint(
            condition=(models.Q(old_reviews=True, length__isnull=True) | models.Q(length__gte=1, length__isnull=False)),
            name="review_length_required_unless_old",
        )]

    def __str__(self):
        # Bez tożsamości autora w etykietach relacji, logach i adminie.
        return self.title

    @property
    def is_copied_to_text(self):
        return self.copied_text_id is not None

    @property
    def author_was_notified(self):
        return self.author_notified_at is not None

    def clean(self):
        super().clean()
        if not self.old_reviews:
            errors = {}
            for field in ("length", "genre", "email"):
                if not getattr(self, field):
                    errors[field] = "Pole wymagane dla bieżącego zgłoszenia."
            if errors:
                raise ValidationError(errors)

    @property
    def display_authors(self):
        authors = [self.author] if self.author_id else []
        if self.pk:
            authors.extend(a for a in self.coauthors.all() if a.pk != self.author_id)
        return authors

    @property
    def author_display_name(self):
        return ", ".join(a.display_name for a in self.display_authors) or self.author_pseudonym.strip() or f"{self.author_first_name} {self.author_last_name}".strip()


class Reviewers(models.Model):
    """Uwagi ogólne do ocen; przydziały przechowuje ReviewAssignment."""
    objects = VersionedQuerySet.as_manager()

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


class ReviewAssignmentQuerySet(VersionedQuerySet):
    def submitted(self):
        return self.exclude(opinion__in=("", "reading"))

    def current(self):
        return self.filter(review__old_reviews=False)

    def for_statistics(self):
        return self.filter(
            models.Q(review__old_reviews=False, review__is_hidden=False)
            | (models.Q(review__old_reviews=True) & ~models.Q(opinion__in=("", "reading")))
        )

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

    historical_person = models.ForeignKey(
        Person, on_delete=models.PROTECT, null=True, blank=True,
        related_name="historical_review_assignments", verbose_name="recenzent historyczny",
        help_text="Profil osoby bez wymogu posiadania konta; tylko dla archiwum.",
    )

    @property
    def reviewer_display_name(self):
        if self.historical_person_id:
            return str(self.historical_person)
        if self.user_id:
            return self.user.get_full_name() or "Nieuzupełnione dane"
        return "Usunięte konto"

    def clean(self):
        super().clean()
        if self.review_id and self.review.old_reviews:
            from texts.archive_dates import validate_archive_dates
            validate_archive_dates(self.assigned_at, self.opinion_changed_at)
        if self.historical_person_id:
            if self.review_id and not self.review.old_reviews:
                raise ValidationError({"historical_person": "Profil historyczny jest dostępny tylko w archiwum."})
            if self.user_id and self.historical_person.user_id != self.user_id:
                raise ValidationError({"historical_person": "Profil nie należy do wskazanego konta."})

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
        default=timezone.now, editable=False, null=True, blank=True,
    )

    opinion_changed_at = models.DateField(
        "data zmiany opinii",
        default=timezone.localdate,
        editable=False, null=True, blank=True,
    )

    objects = ReviewAssignmentQuerySet.as_manager()

    class Meta:
        verbose_name = "przydział recenzenta"
        verbose_name_plural = "przydziały recenzentów"
        ordering = ("position", "pk")
        constraints = [
            models.UniqueConstraint(fields=("review", "historical_person"), name="unique_historical_reviewer"),
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
            and not (self.review_id and self.review.old_reviews)
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
    objects = VersionedQuerySet.as_manager()
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
        return f'{self.recruitment} – {self.full_name}'


from .catalog_models import NovelProfile, VocabularyTerm  # noqa: E402,F401
