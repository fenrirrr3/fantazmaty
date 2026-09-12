from django.conf import settings
from django.db import models
from django.utils import timezone


class AnthologyCorrection(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Nowa"
        CHECKING = "checking", "W sprawdzaniu"
        ACCEPTED = "accepted", "Przyjęta"
        REJECTED = "rejected", "Odrzucona"
        APPLIED = "applied", "Wprowadzona"

    anthology = models.ForeignKey("texts.Anthology", on_delete=models.PROTECT, verbose_name="antologia", related_name="corrections")
    text = models.ForeignKey("texts.Text", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="opowiadanie", related_name="anthology_corrections")
    story_title = models.CharField("tytuł opowiadania", max_length=255)
    fragment = models.TextField("fragment")
    problem = models.TextField("co jest źle")
    suggestion = models.TextField("propozycja poprawki")
    status = models.CharField("status zmiany", max_length=20, choices=Status.choices, default=Status.NEW)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="zgłaszający")
    created_at = models.DateTimeField("zgłoszono", auto_now_add=True)
    updated_at = models.DateTimeField("zmieniono", auto_now=True)
    submission_key = models.UUIDField(null=True, unique=True, editable=False)

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "uwaga do antologii"
        verbose_name_plural = "uwagi do antologii"

    def __str__(self):
        return f"{self.anthology} – {self.story_title}"


class Recruitment(models.Model):
    class Status(models.TextChoices):
        NEW = 'new', 'Czeka na ocenę'
        ACCEPTED = 'accepted', 'Przyjęte'
        REJECTED = 'rejected', 'Odrzucone'

    class Department(models.TextChoices):
        EDITORS = 'editors', 'Redaktorzy'
        PROOFREADERS = 'proofreaders', 'Korektorzy'
        VERIFIERS = 'verifiers', 'Weryfikatorzy'
        REVIEWERS = 'reviewers', 'Recenzenci'
        POST_LAYOUT = 'post_layout', 'Korektorzy poskładowi'
        AUDIO_PROOFREADERS = 'audio_proofreaders', 'Korektorzy audiobooków'
        NARRATORS = 'narrators', 'Lektorzy'
        SOUND = 'sound', 'Dźwiękowcy'
        ILLUSTRATORS = 'illustrators', 'Ilustratorzy'
        TYPESETTERS = 'typesetters', 'Składacze'
        DESIGNERS = 'designers', 'Graficy'
        OTHER = 'other', 'Inne'

    first_name = models.CharField('imię', max_length=150, default='')
    last_name = models.CharField('nazwisko', max_length=150, default='')
    department = models.CharField('dział', max_length=30, choices=Department.choices, default=Department.OTHER)
    submitted_at = models.DateField('data nadesłania', default=timezone.localdate)
    notified = models.BooleanField('czy powiadomiono', default=False)
    notified_at = models.DateTimeField('data powiadomienia', null=True, blank=True, editable=False)
    unofficial_notes = models.TextField('uwagi nieoficjalne', blank=True)
    email = models.EmailField('adres e-mail')
    status = models.CharField('status', max_length=20, choices=Status.choices, default=Status.NEW)
    notes = models.TextField('uwagi', blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'zgłoszenie rekrutacyjne'
        verbose_name_plural = 'rekrutacja'
        ordering = ('-submitted_at', '-pk')

    def clean(self):
        self.email = self.email.strip().lower()
        self.first_name = " ".join(self.first_name.split())
        self.last_name = " ".join(self.last_name.split())

    def save(self, *args, **kwargs):
        from django.db import router, transaction
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            previous = type(self).objects.using(using).select_for_update().filter(pk=self.pk).first() if self.pk else None
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                update_fields = set(update_fields)
                if not update_fields:
                    return
                if previous and 'notified' not in update_fields:
                    self.notified = previous.notified
            if self.notified:
                self.notified_at = previous.notified_at if previous and previous.notified else timezone.now()
            else:
                self.notified_at = None
            if update_fields is not None:
                update_fields.add('notified_at')
                kwargs['update_fields'] = update_fields
            return super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.full_name
