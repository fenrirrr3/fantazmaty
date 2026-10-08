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

    @property
    def is_resolved(self):
        return self.status in (self.Status.APPLIED, self.Status.REJECTED)

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

    applicant_name = models.CharField('imię i nazwisko z formularza', max_length=300, blank=True)
    decision_reason = models.TextField('uzasadnienie decyzji', blank=True)
    mail_subject = models.CharField('temat wiadomości', max_length=2000, blank=True)
    mail_sender = models.CharField('nadawca wiadomości', max_length=2000, blank=True)
    mail_body = models.TextField('treść wiadomości', blank=True)
    mail_received_at = models.DateTimeField('data wiadomości', null=True, blank=True)
    mail_roles = models.JSONField('wybrane role', default=list, blank=True)
    mail_fingerprint = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)

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
        return f"{self.first_name} {self.last_name}".strip() or self.applicant_name

    @property
    def accepted(self):
        return {self.Status.ACCEPTED: True, self.Status.REJECTED: False}.get(self.status)

    @property
    def subject_name(self):
        from core.recruitment_message import name_in_subject, form_fields
        return name_in_subject(self.mail_subject) or self.full_name or form_fields(self.mail_body).get('name') or ''

    @property
    def decision_display(self):
        return {self.Status.ACCEPTED: 'Przyjęty', self.Status.REJECTED: 'Odrzucony'}.get(self.status, 'Bez decyzji')

    @property
    def mail_roles_display(self):
        from core.recruitment_roles import ROLE_CHOICES
        return ', '.join(label for key, label in ROLE_CHOICES if key in self.mail_roles)

    def __str__(self):
        return self.full_name or self.mail_subject or self.mail_sender


class UserActivity(models.Model):
    source_key = models.CharField(max_length=32, unique=True, null=True, blank=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='cms_activities', verbose_name='użytkownik')
    actor = models.CharField('konto', max_length=254)
    created_at = models.DateTimeField('czas', auto_now_add=True, db_index=True)
    method = models.CharField('metoda', max_length=10)
    action = models.CharField('działanie', max_length=255)
    target = models.CharField('obiekt', max_length=255, blank=True)
    path = models.CharField('ścieżka', max_length=1000)
    status_code = models.PositiveSmallIntegerField('wynik HTTP')

    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [models.Index(fields=['user', 'created_at', 'id'], name='activity_user_time_idx')]
        verbose_name = 'aktywność użytkownika'
        verbose_name_plural = 'aktywności użytkowników'

    def __str__(self):
        return f'{self.actor}: {self.action}'




class WorkflowEvent(models.Model):
    personal_work = models.BooleanField(default=False, db_index=True, editable=False)
    personal_work_description = models.CharField(max_length=255, blank=True, editable=False)
    text = models.ForeignKey('texts.Text', null=True, on_delete=models.SET_NULL)
    title = models.TextField()
    authors = models.TextField(blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_name = models.CharField(max_length=300)
    previous_status = models.CharField(max_length=100)
    next_status = models.CharField(max_length=100)
    details = models.TextField(blank=True)
    channel = models.CharField(max_length=100)
    sending_started_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    status = models.CharField(max_length=12, default='pending', choices=(
        ('pending', 'Oczekuje'), ('sending', 'Wysyłanie / brak potwierdzenia'),
        ('sent', 'Wysłano'), ('failed', 'Błąd wysyłki'), ('unknown', 'Brak potwierdzenia'),
        ('disabled', 'Brak skonfigurowanego kanału'),
    ))
    message_id = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [models.Index(fields=['actor', 'created_at', 'id'], name='workflow_actor_time_idx')]
        verbose_name = 'zmiana workflow'
        verbose_name_plural = 'zmiany workflow i powiadomienia Discord'


class EditRevision(models.Model):
    """Internal optimistic-lock counter, independent of domain history."""
    model_label = models.CharField(max_length=100)
    object_id = models.PositiveBigIntegerField()
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('model_label', 'object_id'), name='unique_edit_revision')]


class MailboxConnection(models.Model):
    class Purpose(models.TextChoices):
        SUBMISSIONS = 'submissions', 'Zgłoszenia tekstów'
        RECRUITMENT = 'recruitment', 'Rekrutacja do zespołu'

    purpose = models.CharField('przeznaczenie', max_length=20, choices=Purpose.choices, default=Purpose.SUBMISSIONS)
    class Security(models.TextChoices):
        SSL = 'ssl', 'SSL/TLS (zwykle port 993)'
        STARTTLS = 'starttls', 'STARTTLS (zwykle port 143)'

    name = models.CharField('nazwa skrzynki', max_length=120)
    host = models.CharField('serwer IMAP', max_length=253, help_text='Nazwa serwera, np. imap.example.com, bez https://.')
    port = models.PositiveIntegerField('port', default=993)
    security = models.CharField('szyfrowanie połączenia', max_length=8, choices=Security.choices, default=Security.SSL)
    username = models.CharField('login', max_length=254)
    encrypted_password = models.TextField(editable=False)
    recruitment_subjects = models.TextField('nabory do filtrowania tematów', blank=True, default='',
        help_text='Każdy nabór w osobnym wierszu, bez cudzysłowów i bez prefiksu Nabór:, np. Na pokład, psubraty. Filtr dopasuje temat zawierający Nabór: „Na pokład, psubraty”.')
    folder = models.CharField('folder', max_length=255, default='INBOX', help_text='Nazwa folderu IMAP. Standardowa skrzynka odbiorcza: INBOX.')
    is_active = models.BooleanField('aktywna', default=True)

    class Meta:
        verbose_name = 'skrzynka zgłoszeń'
        verbose_name_plural = 'skrzynki zgłoszeń'
        ordering = ('name', 'pk')

    def __str__(self):
        return self.name

    def clean(self):
        import re
        from django.core.exceptions import ValidationError
        super().clean()
        if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', self.host or ''):
            raise ValidationError({'host': 'Podaj nazwę serwera IMAP bez protokołu, portu i ścieżki.'})
        if self.port is None or not 1 <= self.port <= 65535:
            raise ValidationError({'port': 'Port musi być liczbą od 1 do 65535.'})
        subjects = self.subject_choices()
        if len(subjects) > 200 or any(len(title) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in title) for title in subjects):
            raise ValidationError({'recruitment_subjects': 'Maksymalnie 200 nazw, każda do 255 znaków, bez znaków sterujących.'})
        self.recruitment_subjects = '\n'.join(subjects)
        for field in ('folder', 'username'):
            if any(ord(c) < 32 or ord(c) == 127 for c in getattr(self, field, '')):
                raise ValidationError({field: 'Usuń znaki sterujące.'})

    def subject_choices(self):
        return list(dict.fromkeys(line.strip() for line in self.recruitment_subjects.splitlines() if line.strip()))

    def set_password(self, value):
        from core.mailbox_crypto import encrypt_password
        self.encrypted_password = encrypt_password(value)

    def get_password(self):
        from core.mailbox_crypto import decrypt_password
        return decrypt_password(self.encrypted_password)


class MailboxDownload(models.Model):
    """Local receipt only. Never changes IMAP flags or message contents."""
    mailbox_key = models.CharField(max_length=64)
    uid_validity = models.PositiveBigIntegerField()
    uid = models.PositiveBigIntegerField()
    downloaded_at = models.DateTimeField(auto_now_add=True)
    fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    message_id = models.CharField(max_length=998, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('mailbox_key', 'uid_validity', 'uid'), name='unique_mailbox_download')]


class RecruitmentMailSource(models.Model):
    """IMAP identity is independent of the candidate and its decision."""
    recruitment = models.ForeignKey(Recruitment, on_delete=models.CASCADE, related_name='mail_sources')
    mailbox_key = models.CharField(max_length=64)
    uid_validity = models.PositiveBigIntegerField()
    uid = models.PositiveBigIntegerField()
    downloaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('mailbox_key', 'uid_validity', 'uid'), name='unique_recruitment_mail_source')]


class NewsletterConsent(models.Model):
    """Separate opt-in register; never joined to public author/team projections."""
    email = models.EmailField('adres e-mail', unique=True)
    premieres = models.BooleanField('newsletter o premierach', default=False)
    recruitment = models.BooleanField('newsletter o naborach', default=False)
    updated_at = models.DateTimeField('ostatnia aktualizacja', auto_now=True)

    class Meta:
        ordering = ('email', 'pk')
        verbose_name = 'zgoda newsletterowa'
        verbose_name_plural = 'zgody newsletterowe'

    def __str__(self):
        return f'Zgody newsletterowe #{self.pk}'


class AuthenticationAttempt(models.Model):
    """Short-lived HMAC keys; never store passwords, e-mails or raw IPs."""
    key = models.CharField(max_length=64, unique=True)
    attempts = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)
