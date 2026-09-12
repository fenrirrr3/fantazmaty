import uuid
from django import forms
from django.urls import reverse
from texts.models import Text
from core.models import AnthologyCorrection


class CorrectionForm(forms.ModelForm):
    submission_token = forms.UUIDField(required=False, widget=forms.HiddenInput, initial=uuid.uuid4)
    text = forms.ModelChoiceField(label="Tytuł opowiadania", queryset=Text.objects.none(), required=False, empty_label="Inne miejsce")

    class Meta:
        model = AnthologyCorrection
        fields = ("anthology", "text", "fragment", "problem", "suggestion")
        widgets = {name: forms.Textarea(attrs={"rows": 3}) for name in ("fragment", "problem", "suggestion")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["text"].widget.attrs["data-texts-url"] = reverse("core:correction_texts")
        anthology = self.data.get(self.add_prefix("anthology")) if self.is_bound else self.initial.get("anthology", self.instance.anthology_id)
        if str(anthology or "").isdecimal() and len(str(anthology)) < 19:
            self.fields["text"].queryset = Text.objects.filter(anthology_id=anthology).order_by("title", "pk")

    def clean(self):
        data = super().clean()
        text = data.get("text")
        self.instance.story_title = text.title if text else "Inne miejsce"
        return data


class AdminCorrectionForm(CorrectionForm):
    class Meta(CorrectionForm.Meta):
        fields = (*CorrectionForm.Meta.fields, "status", "submitted_by")
