"""Mikołajki: edycje zabawy i zgłoszenia uczestników (bez kont w CMS-ie)."""
from django.db import models
from django.utils import timezone

LETTER_LIMIT = 5000


class Edition(models.Model):
    """Edycja zabawy (rok). Strona zapisów pokazuje najnowszą edycję."""
    year = models.PositiveSmallIntegerField('rok', unique=True)
    signup_deadline = models.DateTimeField(
        'koniec zapisów', help_text='Po tej dacie i godzinie zapisy zamykają się same.')
    closed_at = models.DateTimeField('zapisy zamknięte ręcznie', null=True, blank=True, editable=False)
    drawn_at = models.DateTimeField('data losowania', null=True, blank=True, editable=False)

    class Meta:
        ordering = ('-year',)
        verbose_name = 'edycja Mikołajek'
        verbose_name_plural = 'Edycje Mikołajek'

    def __str__(self):
        return f'Mikołajki {self.year}'

    @classmethod
    def current(cls):
        """Najnowsza edycja albo None."""
        return cls.objects.order_by('-year').first()

    def signups_open(self, now=None):
        return self.closed_at is None and self.drawn_at is None and (now or timezone.now()) < self.signup_deadline

    @property
    def status_label(self):
        if self.drawn_at:
            return 'Rozlosowano'
        return 'Zapisy otwarte' if self.signups_open() else 'Zapisy zamknięte'


class Entry(models.Model):
    """Zgłoszenie: dane do wysyłki, list do pary i kod uczestnika (do podglądu zgłoszenia na stronie)."""

    class Source(models.TextChoices):
        FORM = 'form', 'Formularz na stronie'
        PANEL = 'panel', 'Dodane w panelu'

    edition = models.ForeignKey(Edition, on_delete=models.PROTECT, related_name='entries', verbose_name='edycja')
    email = models.EmailField('adres e-mail')
    full_name = models.CharField('imię i nazwisko', max_length=200)
    shipping_address = models.TextField('adres lub numer paczkomatu', max_length=500)
    phone = models.CharField('numer telefonu', max_length=30)
    letter = models.TextField('list do pary', max_length=LETTER_LIMIT)
    code = models.CharField('kod uczestnika', max_length=19, unique=True, editable=False)
    source = models.CharField('źródło', max_length=10, choices=Source.choices, default=Source.FORM, editable=False)
    consent_at = models.DateTimeField('zgoda na przetwarzanie danych', null=True, blank=True, editable=False)
    confirmed_at = models.DateTimeField('potwierdzone', null=True, blank=True, editable=False)
    withdrawn_at = models.DateTimeField('wycofane', null=True, blank=True, editable=False)
    recipient = models.OneToOneField('self', on_delete=models.PROTECT, null=True, blank=True, editable=False,
                                     related_name='giver', verbose_name='wylosowana para (komu przygotowuje paczkę)')
    letter_sent_at = models.DateTimeField('list pary wysłany', null=True, blank=True, editable=False)
    package_prepared_at = models.DateTimeField('paczka przygotowana', null=True, blank=True, editable=False)
    address_sent_at = models.DateTimeField('adres pary wysłany', null=True, blank=True, editable=False)
    created_at = models.DateTimeField('utworzone', auto_now_add=True)
    updated_at = models.DateTimeField('ostatnia zmiana', auto_now=True)

    class Meta:
        ordering = ('edition', 'full_name', 'pk')
        verbose_name = 'zgłoszenie do Mikołajek'
        verbose_name_plural = 'Zgłoszenia do Mikołajek'
        constraints = [models.UniqueConstraint(fields=('edition', 'email'), name='unique_mikolajki_email')]

    def __str__(self):
        return f'{self.full_name} ({self.edition})'

    @property
    def active(self):
        """Potwierdzone i niewycofane – bierze udział w losowaniu."""
        return self.confirmed_at is not None and self.withdrawn_at is None

    @property
    def status_label(self):
        if self.withdrawn_at:
            return 'Wycofane'
        if not self.confirmed_at:
            return 'Niepotwierdzone'
        if self.address_sent_at:
            return 'Adres pary wysłany'
        if self.package_prepared_at:
            return 'Paczka przygotowana'
        if self.recipient_id:
            return 'Rozlosowane'
        return 'Potwierdzone'
