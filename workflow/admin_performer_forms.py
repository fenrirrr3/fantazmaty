from django import forms
from django.contrib import admin
from django.contrib.admin.widgets import AutocompleteSelect
from django.contrib.auth import get_user_model
from django.forms.models import BaseInlineFormSet
from texts.models import Text
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.catalog import all_stage_roles
from workflow.admin_performers import performer_plan, correct_stage_performers
from core.edit_versions import version_of


class PerformerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        name = user.get_full_name() or user.get_username()
        return f'{name} — {user.email}' if user.email and name != user.email else name


class WorkflowPerformerForm(forms.ModelForm):
    performer = PerformerChoiceField(
        label='Wykonawca', required=False, queryset=get_user_model().objects.order_by('last_name','first_name','pk'),
        widget=AutocompleteSelect(WorkflowRoleAssignment._meta.get_field('assigned_to'), admin.site),
    )
    workflow_version = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = WorkflowStage
        fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            stage = self.instance
            assignment = stage.assignment
            role = all_stage_roles().get(stage.stage_type)
            if assignment is None and stage.is_current and role:
                assignment = WorkflowRoleAssignment.objects.filter(text_id=stage.text_id,workflow_cycle=stage.workflow_cycle,role=role,is_current=True).first()
            self.fields['performer'].initial = assignment.assigned_to_id if assignment else None
            self.fields['workflow_version'].initial = version_of(stage.text)
            if role is None:
                self.fields['performer'].disabled = True


class WorkflowPerformerFormSet(BaseInlineFormSet):
    def performer_changes(self):
        return {form.instance.pk: form.cleaned_data['performer'] for form in self.forms
                if form.instance.pk and form.cleaned_data and 'performer' in form.changed_data}

    def clean(self):
        super().clean()
        if any(self.errors) or not self.instance.pk:
            return
        changes = self.performer_changes()
        if not changes:
            return
        text = Text.objects.select_for_update().get(pk=self.instance.pk)
        version = version_of(text)
        if any(f.cleaned_data['workflow_version'] != version for f in self.forms if f.instance.pk in changes):
            raise forms.ValidationError('Workflow zmienił się od otwarcia strony. Odśwież formularz.')
        try:
            performer_plan(text, changes)
        except forms.ValidationError as exc:
            raise forms.ValidationError(exc.messages)

    def save_performers(self, actor):
        changes = self.performer_changes()
        self.new_objects, self.deleted_objects = [], []
        self.changed_objects = [(f.instance, ['performer']) for f in self.forms if f.instance.pk in changes]
        if changes:
            correct_stage_performers(self.instance.pk,changes,actor,version_of(self.instance))
        return [f.instance for f in self.forms if f.instance.pk in changes]
