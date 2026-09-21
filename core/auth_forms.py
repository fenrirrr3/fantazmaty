from django import forms
from django.contrib.auth.forms import AuthenticationForm


def configure_email_login(form):
    form.fields['username'] = forms.EmailField(
        label='Adres e-mail', max_length=254,
        widget=forms.EmailInput(attrs={'autofocus': True, 'autocomplete': 'username'}),
    )


class EmailAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_email_login(self)
