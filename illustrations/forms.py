from django import forms

from .models import CoverProposal


class CoverProposalForm(forms.ModelForm):
    class Meta:
        model = CoverProposal

        fields = (
            "illustration_author",
            "illustration_url",
        )

        widgets = {
            "illustration_author": forms.TextInput(
                attrs={
                    "class": "filter-input",
                    "placeholder": (
                        "Imię i nazwisko lub pseudonim"
                    ),
                }
            ),
            "illustration_url": forms.URLInput(
                attrs={
                    "class": "filter-input",
                    "placeholder": "https://...",
                }
            ),
        }