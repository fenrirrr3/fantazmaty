from core.translation_scope import frontend_scope
import uuid
from django import forms
from django.urls import reverse
from texts.models import Text, Anthology
from core.models import AnthologyCorrection


class CorrectionForm(forms.ModelForm):
    submission_token = forms.UUIDField(required=False, widget=forms.HiddenInput, initial=uuid.uuid4)
    text = forms.ModelChoiceField(label="Tytuł opowiadania", queryset=frontend_scope(Text.objects).none(), required=False, empty_label="Inne miejsce")

    class Meta:
        model = AnthologyCorrection
        fields = ("anthology", "text", "fragment", "problem", "suggestion")
        widgets = {name: forms.Textarea(attrs={"rows": 3}) for name in ("fragment", "problem", "suggestion")}

    required_fields = ("fragment", "problem", "suggestion")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.required_fields:
            self.fields[name].required = True
        from django.db.models import Q
        current = Q(pk=self.instance.anthology_id) if self.instance.anthology_id else Q(pk__in=[])
        # Dotychczasowa antologia zostaje do wyboru, nawet gdy nie jest już wydana.
        self.fields["anthology"].queryset = frontend_scope(Anthology.objects).filter(
            Q(status__in=("ready",)) | current).order_by("title", "pk")
        self.fields["text"].widget.attrs["data-texts-url"] = reverse("core:correction_texts")
        anthology = self.data.get(self.add_prefix("anthology")) if self.is_bound else self.initial.get("anthology", self.instance.anthology_id)
        if str(anthology or "").isdecimal() and len(str(anthology)) < 19:
            self.fields["text"].queryset = frontend_scope(Text.objects).filter(anthology_id=anthology).order_by("title", "pk")

    def clean(self):
        data = super().clean()
        text = data.get("text")
        if text:
            self.instance.story_title = text.title
        elif not (self.instance.pk and self.instance.text_id is None and self.instance.story_title):
            self.instance.story_title = "Inne miejsce"
        # Bez opowiadania zostaje dotychczasowe miejsce, np. z importu („Audiodeskrypcja”).
        return data


class AdminCorrectionForm(CorrectionForm):
    required_fields = ()

    class Meta(CorrectionForm.Meta):
        fields = (*CorrectionForm.Meta.fields, "status", "submitted_by", "reporter_name")
