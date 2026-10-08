from datetime import date
from django import forms

DEFAULT_SENT_SINCE = date(2026, 10, 2)


class MailboxDateForm(forms.Form):
    date_filter_enabled = forms.BooleanField(label='Pobieraj wiadomości wysłane od', initial=True, required=False)
    sent_since = forms.DateField(label='Data początkowa (włącznie)', initial=DEFAULT_SENT_SINCE, required=False,
        input_formats=['%Y-%m-%d'], widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))

    def clean(self):
        data = super().clean()
        if data.get('date_filter_enabled') and not data.get('sent_since'):
            self.add_error('sent_since', 'Podaj poprawną datę początkową.')
        return data

    def effective_date(self):
        return self.cleaned_data['sent_since'].isoformat() if self.cleaned_data.get('date_filter_enabled') else ''

    def snapshot(self):
        return {'date_filter_enabled': self.cleaned_data.get('date_filter_enabled', False),
                'sent_since': (self.cleaned_data.get('sent_since') or DEFAULT_SENT_SINCE).isoformat()}
