"""An explicit yes/no decision: an omitted field must not mean rejection."""
from django import forms


def editorial_decision_field(*, required=True):
    return forms.TypedChoiceField(
        label='Co dalej z tekstem?',
        choices=(('true', 'Przekaż do pierwszej korekty'),
                 ('false', 'Potrzebna dalsza redakcja')),
        coerce=lambda value: value == 'true', empty_value=None,
        required=required, widget=forms.RadioSelect,
        error_messages={'required': 'Wybierz dalszą redakcję albo pierwszą korektę.'},
    )
