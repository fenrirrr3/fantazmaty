"""Konfigurowalne powiadomienia Discord: reguły (kanał, oznaczenie roli, treści) i kolejka wysyłek."""
import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from core.edit_versions import VersionedQuerySet

ROLE_NAME_RE = re.compile(r'[0-9A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż_.-]{1,50}')
TEMPLATE_TAGS = {
    'tytuł': 'tytuł tekstu',
    'autor': 'autorzy tekstu jak w stopce (pseudonim albo imię i nazwisko, kilku – po przecinku); '
             'przy zgłoszeniach do recenzji „(anonimowo)”',
    'antologia': 'tytuł antologii',
    'gatunek': 'gatunek',
    'długość': 'długość w znakach ze spacjami',
    'etap': 'nazwa etapu',
    'zdarzenie': '„Rozpoczęto” albo „Zakończono” (tylko „Rozpoczęcie i zakończenie etapu”)',
    'dni': 'ile dni tekst czeka na osobę (przypomnienia)',
    'osoba': 'wykonawca rozpoczętego lub zakończonego etapu (przy „Rozpoczęcie i zakończenie etapu”), kto '
             'wprowadził zmianę (przy „Produkcja audiobooka”) albo kto dodał wpis do kalendarza Social Media – '
             'imię i nazwisko z profilu',
    'zmiana': 'opis zmiany w produkcji audiobooka, np. „Zmieniono status: Montaż.” (tylko „Produkcja audiobooka”)',
    'status': 'status audiobooka po zmianie (tylko „Produkcja audiobooka”)',
    'data': 'dzień wpisu w kalendarzu Social Media, np. „czwartek, 5.11.2026” (tylko „Nowy wpis w kalendarzu '
            'Social Media”)',
    'opis': 'co idzie – krótka informacja z wpisu w kalendarzu Social Media',
    'platforma': 'gdzie idzie wpis z kalendarza Social Media (Facebook, Instagram…)',
    'rola': 'oznaczenie roli (bez tego tagu trafia na początek wiadomości)',
    'link': 'odnośnik „O, tutaj” do podglądu tekstu w CMS-ie – przy audiobookach do strony audiobooka, przy '
            'kalendarzu Social Media do dnia w kalendarzu (bez tego tagu trafia na koniec wiadomości)',
}
TAG_RE = re.compile(r'\[([^\[\]\n]{1,30})\]')


def validate_template_tags(value):
    unknown = sorted({tag for tag in TAG_RE.findall(value) if tag.lower() not in TEMPLATE_TAGS})
    if unknown:
        raise ValidationError('Nieznane tagi: %(tags)s. Dostępne: %(allowed)s.', params={
            'tags': ', '.join(f'[{tag}]' for tag in unknown),
            'allowed': ', '.join(f'[{tag}]' for tag in TEMPLATE_TAGS)})


class DiscordRole(models.Model):
    """Nazwa używana w regułach (@redaktor) → identyfikator roli na serwerze Discorda.

    Ta sama ranga może być nadawana według ról w CMS-ie (people.discord_sync): cms_roles to role, których
    członkowie ekipy mają ją mieć, a sync_mode – czy CMS ją tylko nadaje, czy także odbiera. Pełna synchronizacja
    działa dopiero po zatwierdzeniu podglądu dla bieżącego mapowania (full_sync_approved_for); zmiana ID rangi
    albo ról CMS unieważnia zatwierdzenie – do ponownego zatwierdzenia ranga jest tylko nadawana.
    """

    class SyncMode(models.TextChoices):
        OFF = 'off', 'Wyłączona (tylko oznaczenia w powiadomieniach)'
        GRANT = 'grant', 'Tylko nadawanie (dodaje brakujących, nikomu nie zabiera)'
        FULL = 'full', 'Pełna synchronizacja (nadaje i zabiera)'

    objects = VersionedQuerySet.as_manager()
    name = models.CharField('nazwa (bez @)', max_length=50, unique=True,
                            validators=[RegexValidator(ROLE_NAME_RE, 'Nazwa: litery, cyfry, kropka, podkreślnik lub dywiz.')],
                            help_text='Np. redaktor – w regule wpiszesz wtedy @redaktor.')
    role_id = models.CharField('ID roli na Discordzie', max_length=25,
                               validators=[RegexValidator(r'^[0-9]{5,25}$', 'Wpisz sam numer ID roli.')],
                               help_text='Discord: Ustawienia serwera → Role → prawy przycisk na roli → Kopiuj ID roli '
                                         '(wymaga włączonego trybu dewelopera).')
    cms_roles = models.ManyToManyField('people.Role', blank=True, related_name='discord_roles',
                                       verbose_name='role w CMS-ie',
                                       help_text='Członkowie ekipy z którąkolwiek z tych ról mają mieć tę rangę.')
    sync_mode = models.CharField('nadawanie rangi przez CMS', max_length=10, choices=SyncMode.choices,
                                 default=SyncMode.OFF)
    full_sync_approved_for = models.CharField(max_length=255, blank=True, editable=False)
    full_sync_approved_at = models.DateTimeField('pełna synchronizacja zatwierdzona', null=True, blank=True,
                                                 editable=False)
    full_sync_approved_by = models.ForeignKey('auth.User', null=True, blank=True, on_delete=models.SET_NULL,
                                              related_name='+', editable=False, verbose_name='zatwierdził')

    class Meta:
        ordering = ('name',)
        verbose_name = 'rola Discorda'
        verbose_name_plural = 'role Discorda'

    def __str__(self):
        return f'@{self.name}'

    def mapping_key(self, role_ids=None):
        """Mapowanie, którego dotyczy zatwierdzenie: ID rangi i role CMS (zmiana któregoś unieważnia zatwierdzenie)."""
        if role_ids is None:
            role_ids = [role.pk for role in self.cms_roles.all()] if self.pk else ()
        return f'{self.role_id}:' + ','.join(str(pk) for pk in sorted(role_ids))

    @property
    def full_sync_approved(self):
        return bool(self.full_sync_approved_for) and self.full_sync_approved_for == self.mapping_key()

    def effective_mode(self):
        """Tryb, w którym CMS naprawdę działa: pełna synchronizacja bez ważnego zatwierdzenia = tylko nadawanie."""
        if self.sync_mode == self.SyncMode.FULL and not self.full_sync_approved:
            return self.SyncMode.GRANT
        return self.sync_mode


def validate_discord_invite(value):
    if value and not re.fullmatch(r'https://(discord\.gg|(www\.)?discord\.com/invite)/[A-Za-z0-9-]{2,40}/?', value):
        raise ValidationError('Podaj link zaproszenia Discorda: https://discord.gg/… albo https://discord.com/invite/…')


class DiscordSettings(models.Model):
    """Ustawienia Discorda edytowalne w panelu (jeden rekord). Token bota i ID serwera są tylko w .env."""
    objects = VersionedQuerySet.as_manager()
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    invite_url = models.URLField('link zapraszający na serwer', max_length=200, blank=True,
                                 validators=[validate_discord_invite],
                                 help_text='Np. https://discord.gg/abc123 – trafia do zaproszenia do CMS-u, gdy '
                                           'zaznaczysz „Zaproś też na Discorda”. Ustaw link bez wygasania '
                                           '(Discord: Zaproś ludzi → Edytuj link zaproszenia → Wygasa po: Nigdy). '
                                           'Pusty – opcja zaproszenia na Discorda jest niedostępna.')

    class Meta:
        verbose_name = 'ustawienia Discorda'
        verbose_name_plural = 'Discord – ustawienia (zaproszenie)'
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='single_discord_settings')]

    def __str__(self):
        return 'Discord – link zapraszający'

    @classmethod
    def invite_url_value(cls):
        return cls.objects.filter(pk=1).values_list('invite_url', flat=True).first() or ''


class DiscordRoleChange(models.Model):
    """Historia synchronizacji rang: każda próba nadania lub odebrania rangi przez CMS (tylko do odczytu)."""

    class Action(models.TextChoices):
        GRANT = 'grant', 'Nadanie'
        REVOKE = 'revoke', 'Odebranie'

    class Result(models.TextChoices):
        DONE = 'done', 'Wykonano'
        ERROR = 'error', 'Błąd – do ponowienia'
        RETRIED = 'retried', 'Błąd – ponowiono później'

    class Source(models.TextChoices):
        CHANGE = 'change', 'Zmiana w CMS-ie'
        PANEL = 'panel', 'Synchronizacja z panelu'
        APPROVAL = 'approval', 'Zatwierdzenie pełnej synchronizacji'
        COMMAND = 'command', 'Zadanie okresowe (sync_discord_roles)'

    created_at = models.DateTimeField('czas', auto_now_add=True, db_index=True)
    person = models.ForeignKey('people.Person', null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='discord_role_changes', verbose_name='osoba')
    person_label = models.CharField('osoba (w chwili zmiany)', max_length=210, blank=True)
    discord_user_id = models.CharField('ID użytkownika Discorda', max_length=20, db_index=True)
    discord_role = models.ForeignKey(DiscordRole, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='changes', verbose_name='ranga')
    role_label = models.CharField('ranga (w chwili zmiany)', max_length=60)
    role_id = models.CharField('ID rangi', max_length=25)
    action = models.CharField('operacja', max_length=10, choices=Action.choices)
    result = models.CharField('wynik', max_length=10, choices=Result.choices, db_index=True)
    reason = models.CharField('powód', max_length=255)
    source = models.CharField('wywołanie', max_length=10, choices=Source.choices)
    actor = models.ForeignKey('auth.User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                              verbose_name='kto')
    response = models.CharField('odpowiedź Discorda', max_length=300, blank=True)

    class Meta:
        ordering = ('-created_at', '-pk')
        verbose_name = 'zmiana rangi Discorda'
        verbose_name_plural = 'Discord – historia rang'

    def __str__(self):
        who = self.person_label or self.discord_user_id
        return f'{self.get_action_display()}: {self.role_label} – {who}'


class NotificationRule(models.Model):
    class Trigger(models.TextChoices):
        REVIEW = 'review_new', 'Nowy tekst do recenzji'
        EDITING = 'ready_for_editing', 'Nowy tekst do redakcji'
        FIRST_PROOFREADING = 'first_proofreading', 'Nowy tekst do pierwszej korekty'
        THIRD_PROOFREADING = 'third_proofreading', 'Nowy tekst do trzeciej korekty'
        FIRST_VERIFICATION = 'first_verification', 'Nowy tekst do pierwszej weryfikacji'
        SECOND_VERIFICATION = 'second_verification', 'Nowy tekst do drugiej weryfikacji'
        THIRD_VERIFICATION = 'third_verification', 'Nowy tekst do trzeciej weryfikacji'
        STAGE_COMPLETED = 'stage_completed', 'Rozpoczęcie i zakończenie etapu (wszystkie etapy)'
        STAGE_WAITING = 'stage_waiting', 'Tekst nadal czeka na osobę (po 7, 14 i 21 dniach)'
        AUDIOBOOK = 'audiobook_production', 'Produkcja audiobooka (wszystkie zmiany)'
        SOCIAL_CALENDAR = 'social_calendar', 'Nowy wpis w kalendarzu Social Media'

    objects = VersionedQuerySet.as_manager()
    trigger = models.CharField('kiedy', max_length=32, choices=Trigger.choices, unique=True, editable=False)
    enabled = models.BooleanField('włączone', default=False)
    channel = models.CharField('kanał', max_length=100, blank=True,
                               help_text='Nazwa kanału z konfiguracji webhooków serwera (DISCORD_WEBHOOKS).')
    mention = models.CharField('oznacz rolę', max_length=60, blank=True,
                               help_text='Np. @redaktor (rola z listy „Role Discorda”) albo @here. Puste – bez oznaczenia.')

    class Meta:
        ordering = ('pk',)
        verbose_name = 'powiadomienie Discord'
        verbose_name_plural = 'powiadomienia Discord – ustawienia'

    def __str__(self):
        return self.get_trigger_display()


class NotificationTemplate(models.Model):
    objects = VersionedQuerySet.as_manager()
    rule = models.ForeignKey(NotificationRule, on_delete=models.CASCADE, related_name='templates')
    content = models.TextField('treść', max_length=1500, validators=[validate_template_tags])

    class Meta:
        ordering = ('pk',)
        verbose_name = 'treść wiadomości'
        verbose_name_plural = 'treści wiadomości (losowana jest jedna)'

    def __str__(self):
        return self.content[:60]


class DiscordNotification(models.Model):
    """Kolejka wysyłek – każda wiadomość z reguły trafia tu przed wysłaniem."""
    trigger = models.CharField('zdarzenie', max_length=32, choices=NotificationRule.Trigger.choices, db_index=True)
    channel = models.CharField('kanał', max_length=100)
    content = models.TextField('treść')
    role_ids = models.CharField('oznaczone role (ID)', max_length=200, blank=True)
    mention_here = models.BooleanField('@here', default=False)
    text = models.ForeignKey('texts.Text', null=True, blank=True, on_delete=models.SET_NULL, verbose_name='tekst')
    review = models.ForeignKey('texts.Review', null=True, blank=True, on_delete=models.SET_NULL, verbose_name='zgłoszenie')
    stage = models.ForeignKey('workflow.WorkflowStage', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+', verbose_name='etap')
    reminder_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='reminders',
                                    verbose_name='przypomnienie o wiadomości')
    reminder_days = models.PositiveSmallIntegerField('przypomnienie po (dniach)', null=True, blank=True)
    status = models.CharField('stan', max_length=12, default='pending', choices=(
        ('pending', 'Oczekuje'), ('sending', 'Wysyłanie / brak potwierdzenia'), ('sent', 'Wysłano'),
        ('failed', 'Błąd wysyłki'), ('unknown', 'Brak potwierdzenia'), ('disabled', 'Brak skonfigurowanego kanału')))
    message_id = models.CharField(max_length=30, blank=True)
    sending_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField('utworzono', auto_now_add=True)

    class Meta:
        ordering = ('-created_at', '-pk')
        # Ponawianie wysyłek i diagnostyka filtrują po stanie i czasie utworzenia.
        indexes = [models.Index(fields=['status', 'created_at'], name='discord_status_time_idx')]
        verbose_name = 'wysłane powiadomienie Discord'
        verbose_name_plural = 'powiadomienia Discord – historia wysyłek'

    def __str__(self):
        return f'{self.get_trigger_display()} → {self.channel}'
