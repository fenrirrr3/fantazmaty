"""Strona WWW: działy ekipy, katalog opowiadań szukajki i antologie strony „Słuchaj” publikowane na fantazmaty.pl
(endpointy z kluczem)."""
import hashlib
import json

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, URLValidator
from django.db import models

from core.edit_versions import VersionedQuerySet
from core.site_note import clean_note

DEFAULT_PERSON = 'Dawid Wiktorski'


class SiteTeamSection(models.Model):
    """Dział na stronie „Ekipa”. Kotwica na stronie to ``ekipa-<slug>``."""
    objects = VersionedQuerySet.as_manager()
    slug = models.SlugField('identyfikator', unique=True,
                            help_text='Kotwica na stronie: ekipa-<identyfikator>. Małe litery, cyfry i myślniki.')
    title = models.CharField('tytuł', max_length=100)
    position = models.PositiveSmallIntegerField('kolejność', default=0)
    is_enabled = models.BooleanField('włączony', default=True,
                                     help_text='Wyłączony dział nie trafia na stronę.')
    show_count = models.BooleanField('liczba osób w nagłówku', default=True)
    coordinator_roles = models.ManyToManyField('people.Role', blank=True, related_name='+',
                                               verbose_name='role koordynatorów',
                                               help_text='Osoby z tymi rolami trafiają do linii „Koordynacja”.')
    manual_coordinators = models.TextField('koordynatorzy wpisani ręcznie', blank=True,
                                           help_text='Po jednym imieniu i nazwisku w wierszu; dopisywani za osobami z ról '
                                                     '(np. koordynator bez konta w CMS-ie).')
    note = models.TextField('notka', blank=True,
                            help_text='Akapit zamiast listy osób (np. Ilustracje). Dozwolone znaczniki: '
                                      '<a href="https://…">, <strong>, <em>; pozostałe są usuwane.')
    in_navigation = models.BooleanField('w nawigacji „Przejdź do działu”', default=True)
    navigation_label = models.CharField('etykieta w nawigacji', max_length=100, blank=True,
                                        help_text='Pusta – tytuł działu.')

    class Meta:
        verbose_name = 'dział na stronie WWW'
        verbose_name_plural = 'Strona WWW – działy ekipy'
        ordering = ('position', 'pk')

    def __str__(self):
        return self.title

    @property
    def anchor(self):
        return f'ekipa-{self.slug}'

    @property
    def manual_coordinator_list(self):
        return [line.strip() for line in self.manual_coordinators.splitlines() if line.strip()]

    def clean(self):
        super().clean()
        self.note = clean_note(self.note)


class SiteTeamGroup(models.Model):
    """Podgrupa listy działu: z ról (automatycznie) albo z ręcznie wpisanych nazwisk."""
    objects = VersionedQuerySet.as_manager()

    class Source(models.TextChoices):
        ROLES = 'roles', 'z ról'
        MANUAL = 'manual', 'ręcznie'

    section = models.ForeignKey(SiteTeamSection, on_delete=models.CASCADE, related_name='groups',
                                verbose_name='dział')
    title = models.CharField('tytuł podgrupy', max_length=100, blank=True,
                             help_text='Pusty przy jednej podgrupie – lista bez nagłówka.')
    position = models.PositiveSmallIntegerField('kolejność', default=0)
    source = models.CharField('źródło', max_length=10, choices=Source.choices, default=Source.ROLES)
    roles = models.ManyToManyField('people.Role', blank=True, related_name='+', verbose_name='role',
                                   help_text='Aktywne, niezewnętrzne osoby z którąkolwiek z ról.')
    manual_names = models.TextField('nazwiska', blank=True,
                                    help_text='Jedno imię i nazwisko w wierszu, w podanej kolejności.')

    class Meta:
        verbose_name = 'podgrupa działu'
        verbose_name_plural = 'podgrupy działu'
        ordering = ('position', 'pk')

    def __str__(self):
        return self.title or f'{self.section} – lista'

    @property
    def manual_list(self):
        return [line.strip() for line in self.manual_names.splitlines() if line.strip()]

    def clean(self):
        super().clean()
        if self.source == self.Source.MANUAL and not self.manual_list:
            raise ValidationError({'manual_names': 'Ręczna podgrupa wymaga co najmniej jednego nazwiska.'})


def term_lines(value):
    """Wiersze pola tekstowego bez pustych i powtórzeń (porównanie bez wielkości liter), w kolejności zapisu."""
    seen, result = set(), []
    for line in (value or '').splitlines():
        name = ' '.join(line.split())
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    return result


class SiteCatalogAnthology(models.Model):
    """Antologia w katalogu szukajki (fantazmaty.pl/czytaj/szukaj/); może nie mieć odpowiednika w CMS-ie."""
    objects = VersionedQuerySet.as_manager()
    anthology = models.OneToOneField('texts.Anthology', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='site_catalog', verbose_name='antologia w CMS-ie',
                                     help_text='Powiązanie pozwala sprawdzać aktualizacje; puste dla antologii '
                                               'spoza CMS-u.')
    title = models.CharField('nazwa na stronie', max_length=255)
    url = models.URLField('adres antologii', max_length=500, blank=True, validators=[URLValidator(schemes=['https'])],
                          help_text='Strona antologii na fantazmaty.pl (https://…).')
    position = models.PositiveSmallIntegerField('kolejność', default=0)
    is_enabled = models.BooleanField('włączona', default=True,
                                     help_text='Wyłączona antologia nie trafia do szukajki.')
    updated_at = models.DateTimeField('ostatnia zmiana', auto_now=True)

    class Meta:
        verbose_name = 'antologia w katalogu szukajki'
        verbose_name_plural = 'Szukajka WWW – katalog opowiadań'
        ordering = ('position', 'pk')

    def __str__(self):
        return self.display_title

    @property
    def display_title(self):
        """Nazwa na stronie: z CMS-u dla powiązanej antologii, inaczej zapisana w katalogu."""
        return linked_title(self)

    def clean(self):
        super().clean()
        if self.url and not self.url.startswith('https://fantazmaty.pl/'):
            raise ValidationError({'url': 'Adres musi zaczynać się od https://fantazmaty.pl/.'})


class SiteCatalogStory(models.Model):
    """Opowiadanie w katalogu szukajki: tytuł i autor jak na stronie, gatunki i tagi po jednym w wierszu.

    Wpis powiązany z tekstem (albo pozycja powieści) dostaje na stronie tytuł i autora z CMS-u, a gatunki i tagi
    z CMS-u dokładane za zapisanymi tutaj (``core.site_catalog.story_entry``); pola tytułu i autora są wtedy tylko
    kopią na wypadek zerwania powiązania.
    """
    objects = VersionedQuerySet.as_manager()
    anthology = models.ForeignKey(SiteCatalogAnthology, on_delete=models.CASCADE, related_name='stories',
                                  verbose_name='antologia w katalogu')
    text = models.OneToOneField('texts.Text', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='site_catalog_story', verbose_name='tekst w CMS-ie')
    title = models.CharField('tytuł', max_length=255)
    author = models.CharField('autor', max_length=255, blank=True,
                              help_text='Tak jak na stronie, np. „Kazimierz Kyrcz Jr, Michał Walczak”.')
    genres = models.TextField('gatunki', blank=True, help_text='Jeden gatunek w wierszu.')
    tags = models.TextField('tagi', blank=True, help_text='Jeden tag w wierszu.')
    position = models.PositiveSmallIntegerField('kolejność', default=0)

    class Meta:
        verbose_name = 'opowiadanie w katalogu'
        verbose_name_plural = 'opowiadania w katalogu'
        ordering = ('position', 'pk')

    def __str__(self):
        return self.title

    @property
    def genre_list(self):
        return term_lines(self.genres)

    @property
    def tag_list(self):
        return term_lines(self.tags)


def linked_title(entry):
    """Nazwa antologii na stronie: ``Anthology.title`` dla wpisu powiązanego z CMS-em, inaczej pole ``title``."""
    return entry.anthology.title if entry.anthology_id else entry.title


def with_version(payload):
    """Słownik dla strony z polem ``wersja`` – skrótem treści (bez czasu), który jest też ETag-iem endpointu:
    te same dane dają tę samą wersję, każda zmiana – inną."""
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {**payload, 'wersja': hashlib.sha256(content.encode()).hexdigest()[:20]}


class SiteTeamSettings(models.Model):
    """Ustawienia strony WWW (jeden rekord, pk=1): osoby ze strony „Ekipa”, adres szukajki i ślady pobrań."""
    objects = VersionedQuerySet.as_manager()
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    lead_editor = models.CharField('redaktor prowadzący', max_length=200, default=DEFAULT_PERSON)
    site_person = models.CharField('strona internetowa', max_length=200, default=DEFAULT_PERSON)
    last_fetched_at = models.DateTimeField('ostatnie pobranie ekipy przez stronę', null=True, blank=True,
                                           editable=False)
    last_fetch_version = models.CharField('wersja ostatniego pobrania ekipy', max_length=40, blank=True,
                                          editable=False)
    catalog_page_url = models.URLField('adres szukajki na stronie', max_length=500, blank=True,
                                       validators=[URLValidator(schemes=['https'])],
                                       help_text='Np. https://fantazmaty.pl/czytaj/szukaj/ – daje odnośnik '
                                                 '„Odśwież stronę”. Puste – bez odnośnika.')
    catalog_fetched_at = models.DateTimeField('ostatnie pobranie katalogu przez stronę', null=True, blank=True,
                                              editable=False)
    catalog_fetch_version = models.CharField('wersja ostatniego pobrania katalogu', max_length=40, blank=True,
                                             editable=False)
    listen_page_url = models.URLField('adres strony „Słuchaj”', max_length=500, blank=True,
                                      validators=[URLValidator(schemes=['https'])],
                                      help_text='Np. https://fantazmaty.pl/sluchaj/ – daje odnośnik „Odśwież stronę”. '
                                                'Puste – bez odnośnika.')
    listen_fetched_at = models.DateTimeField('ostatnie pobranie nagrań przez stronę', null=True, blank=True,
                                             editable=False)
    listen_fetch_version = models.CharField('wersja ostatniego pobrania nagrań', max_length=40, blank=True,
                                            editable=False)

    class Meta:
        verbose_name = 'ustawienia strony WWW'
        verbose_name_plural = 'Strona WWW – ustawienia'
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='single_site_team_settings')]

    def __str__(self):
        return 'Strona WWW – ustawienia'

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def catalog_refresh_url(self):
        """Adres szukajki z ``?fz_odswiez=1`` (pobranie danych od razu przez zalogowanego administratora)."""
        return refresh_url(self.catalog_page_url)

    @property
    def listen_refresh_url(self):
        """Adres strony „Słuchaj” z ``?fz_odswiez=1``."""
        return refresh_url(self.listen_page_url)


def refresh_url(url):
    """Adres strony z ``fz_odswiez=1``; pusty adres – pusty napis."""
    if not url:
        return ''
    return url + ('&' if '?' in url else '?') + 'fz_odswiez=1'


def validate_site_url(value):
    if value and not value.startswith('https://fantazmaty.pl/'):
        raise ValidationError('Adres musi zaczynać się od https://fantazmaty.pl/.')


CARD_ID_RE = r'^[a-zA-Z0-9_-]+$'
DURATION_RE = r'^(?:\d{1,2}:)?\d{1,2}:\d{2}$'


class SiteListenAnthology(models.Model):
    """Antologia na stronie „Słuchaj” (fantazmaty.pl/sluchaj/): identyfikator filtra, nazwa i okładka kart."""
    objects = VersionedQuerySet.as_manager()
    anthology = models.OneToOneField('texts.Anthology', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='site_listen', verbose_name='antologia w CMS-ie',
                                     help_text='Opublikowane audiobooki tekstów tej antologii trafiają na stronę.')
    slug = models.SlugField('identyfikator na stronie', max_length=100, unique=True,
                            help_text='Wartość filtra antologii, np. nanofantazje-30. Małe litery, cyfry i myślniki.')
    title = models.CharField('nazwa na stronie', max_length=255)
    cover_url = models.URLField('adres okładki', max_length=500, blank=True,
                                validators=[URLValidator(schemes=['https']), validate_site_url],
                                help_text='Plik okładki na fantazmaty.pl (https://fantazmaty.pl/wp-content/…). '
                                          'Bez okładki antologia nie trafia na stronę.')
    position = models.PositiveSmallIntegerField('kolejność', default=0)
    is_enabled = models.BooleanField('włączona', default=True, help_text='Wyłączona antologia nie trafia na stronę.')

    class Meta:
        verbose_name = 'antologia na stronie „Słuchaj”'
        verbose_name_plural = 'Słuchaj WWW – antologie i czasy nagrań'
        ordering = ('position', 'pk')

    def __str__(self):
        return self.display_title

    @property
    def display_title(self):
        """Nazwa na stronie: z CMS-u dla powiązanej antologii, inaczej zapisana tutaj."""
        return linked_title(self)


class SiteListenRecording(models.Model):
    """Nagranie (część audiobooka) na stronie „Słuchaj”: stały identyfikator karty i czasy z YouTube i HearThis.

    CMS nie zna czasów nagrań, więc są tutaj. Wpis bez audiobooka to nagranie z dawnej strony, którego nie udało się
    dopasować do CMS-u (pola „ze strony” służą do dopasowania i ostrzeżeń).
    """
    objects = VersionedQuerySet.as_manager()
    listen_anthology = models.ForeignKey(SiteListenAnthology, on_delete=models.CASCADE, related_name='recordings',
                                         verbose_name='antologia na stronie')
    audiobook = models.ForeignKey('core.Audiobook', on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='site_listen_recordings', verbose_name='audiobook w CMS-ie')
    part = models.PositiveSmallIntegerField('część', default=1, help_text='1 – główne nagranie; kolejne części jak '
                                                                          'w linkach audiobooka.')
    card_id = models.CharField('identyfikator karty', max_length=40, unique=True, blank=True,
                               validators=[RegexValidator(CARD_ID_RE, 'Dozwolone litery, cyfry, „-” i „_”.')],
                               help_text='Kotwica karty na stronie; puste – nadawane automatycznie (fza-a<audiobook>-<część>).')
    youtube_duration = models.CharField('czas na YouTube', max_length=8, blank=True,
                                        validators=[RegexValidator(DURATION_RE, 'Czas w formacie m:ss albo g:mm:ss.')])
    hearthis_duration = models.CharField('czas na HearThis', max_length=8, blank=True,
                                         validators=[RegexValidator(DURATION_RE, 'Czas w formacie m:ss albo g:mm:ss.')])
    source_title = models.CharField('tytuł ze strony', max_length=255, blank=True, editable=False)
    source_author = models.CharField('autor ze strony', max_length=255, blank=True, editable=False)
    source_youtube = models.URLField('YouTube ze strony', max_length=1000, blank=True, editable=False)
    source_hearthis = models.URLField('HearThis ze strony', max_length=1000, blank=True, editable=False)

    class Meta:
        verbose_name = 'nagranie na stronie „Słuchaj”'
        verbose_name_plural = 'nagrania i czasy'
        ordering = ('listen_anthology', 'pk')
        constraints = [models.UniqueConstraint(fields=('audiobook', 'part'), name='unique_site_listen_part')]

    def __str__(self):
        return self.card_id or self.source_title or f'nagranie {self.pk}'

    def clean(self):
        super().clean()
        if not self.card_id and not self.audiobook_id:
            raise ValidationError({'card_id': 'Podaj identyfikator karty albo wybierz audiobook.'})
        anthology_id = self.listen_anthology.anthology_id if self.listen_anthology_id else None
        if self.audiobook_id and anthology_id and self.audiobook.text.anthology_id != anthology_id:
            raise ValidationError({'audiobook': 'Audiobook należy do innej antologii.'})

    def save(self, *args, **kwargs):
        if not self.card_id and self.audiobook_id:
            self.card_id = f'fza-a{self.audiobook_id}-{self.part}'
        super().save(*args, **kwargs)
