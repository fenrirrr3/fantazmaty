"""Formularze strony Mikołajek i panelu admina."""
from django import forms

from .models import LETTER_LIMIT, Entry

ENTRY_FIELDS = ('full_name', 'shipping_address', 'phone', 'letter')
PHONE_CHARS = frozenset('0123456789 +-()')


class EntryDataForm(forms.ModelForm):
    """Dane do wysyłki i list – wspólne dla zapisu, zmiany na stronie i dodania w panelu."""

    class Meta:
        model = Entry
        fields = ENTRY_FIELDS
        widgets = {
            'shipping_address': forms.Textarea(attrs={'rows': 3, 'maxlength': 500}),
            'letter': forms.Textarea(attrs={'rows': 10, 'maxlength': LETTER_LIMIT}),
        }
        help_texts = {
            'shipping_address': 'Ulica, numer, kod pocztowy i miejscowość albo numer paczkomatu InPost.',
            'phone': 'Potrzebny przy odbiorze paczki.',
            'letter': f'Napisz, co lubisz, czego nie lubisz, jakie masz pasje – Twoja para przeczyta ten list. '
                      f'Najwyżej {LETTER_LIMIT} znaków.',
        }

    def clean_full_name(self):
        return ' '.join(self.cleaned_data['full_name'].split())

    def clean_shipping_address(self):
        return '\n'.join(line.strip() for line in self.cleaned_data['shipping_address'].strip().splitlines()
                         if line.strip())

    def clean_phone(self):
        phone = ' '.join(self.cleaned_data['phone'].split())
        if not set(phone) <= PHONE_CHARS or sum(char.isdigit() for char in phone) < 9:
            raise forms.ValidationError('Podaj numer telefonu (co najmniej 9 cyfr).')
        return phone

    def clean_letter(self):
        return self.cleaned_data['letter'].strip()


class SignupForm(EntryDataForm):
    """Zapis na stronie: adres e-mail, dane, list, zgoda i pole-pułapka na boty."""
    email = forms.EmailField(label='Adres e-mail', help_text='Na ten adres wyślemy podsumowanie, kod uczestnika '
                                                            'i link potwierdzający.')
    consent = forms.BooleanField(
        label='Zgadzam się na przetwarzanie podanych danych w celu organizacji zabawy mikołajkowej Fantazmatów '
              '(wysyłki paczki przez moją parę i kontaktu w sprawie zabawy).',
        error_messages={'required': 'Zaznacz zgodę – bez niej nie możemy przekazać Twojego adresu parze.'})
    # Pułapka: pole ukryte przed ludźmi, boty wypełniają je automatycznie.
    website = forms.CharField(required=False, label='Strona WWW (zostaw puste)')

    field_order = ('email', *ENTRY_FIELDS, 'consent')

    def clean_email(self):
        return self.cleaned_data['email'].strip().casefold()


class CodeForm(forms.Form):
    """Wejście do własnego zgłoszenia: adres e-mail i kod uczestnika z wiadomości."""
    email = forms.EmailField(label='Adres e-mail')
    code = forms.CharField(label='Kod uczestnika', max_length=40, help_text='Kod z wiadomości, np. ABCD-EFGH-JKMN-PQRS.')


class PanelEntryForm(forms.ModelForm):
    """Ręczne dodanie zgłoszenia w panelu (od razu potwierdzone, z wysłaniem podsumowania)."""

    class Meta:
        model = Entry
        fields = ('edition', 'email', *ENTRY_FIELDS)
        widgets = EntryDataForm.Meta.widgets

    clean_full_name = EntryDataForm.clean_full_name
    clean_shipping_address = EntryDataForm.clean_shipping_address
    clean_phone = EntryDataForm.clean_phone
    clean_letter = EntryDataForm.clean_letter

    def clean_email(self):
        return self.cleaned_data['email'].strip().casefold()

    def clean(self):
        data = super().clean()
        edition = data.get('edition') or getattr(self.instance, 'edition', None)
        if edition is not None and edition.drawn_at and (self.instance.pk is None or 'edition' in self.changed_data):
            raise forms.ValidationError('Pary w tej edycji są już rozlosowane – nie można dodać zgłoszenia.')
        email = data.get('email')
        if edition is not None and email and Entry.objects.filter(edition=edition, email=email) \
                .exclude(pk=self.instance.pk).exists():
            self.add_error('email', 'W tej edycji jest już zgłoszenie z tym adresem e-mail.')
        return data
