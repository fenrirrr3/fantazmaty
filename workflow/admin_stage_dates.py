"""Administrator date corrections and ordinary workflow transitions."""
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.edit_versions import version_of
from core.workflow_events import track_workflow
from texts.models import Text
from workflow.anthology_policy import require_working_anthology
from workflow.models import WorkflowStage as S
from workflow.services import (
    complete_stage, ensure_stage_belongs_to_current_cycle,
    ensure_text_is_not_withdrawn, finish_editing_to_coordinator, resume_editing,
    send_text_to_author, send_to_first_verification, send_to_second_verification,
    validate_assignment_start_date, _validate_date, STAGE_ROLES, current_stage_queryset,
    completed_stage_exists,
)


EDITING_TRANSITIONS = {
    S.StageType.FIRST_VERIFICATION: send_to_first_verification,
    S.StageType.AUTHOR_EDITING: send_text_to_author,
    S.StageType.SECOND_VERIFICATION: send_to_second_verification,
    S.StageType.EDITING_CONTROL: finish_editing_to_coordinator,
}


def editing_transition_choices(text):
    """Completed imported checkpoints count even when their dates are unknown."""
    stages = list(current_stage_queryset(text))
    first_done = completed_stage_exists(text, S.StageType.FIRST_VERIFICATION)
    second_done = completed_stage_exists(text, S.StageType.SECOND_VERIFICATION)
    started = {stage.stage_type for stage in stages
               if not stage.is_completed and stage.started_at is not None}
    existing = {stage.stage_type for stage in stages}
    available = []
    if not first_done and S.StageType.FIRST_VERIFICATION not in started:
        available.append(S.StageType.FIRST_VERIFICATION)
    if first_done and S.StageType.AUTHOR_EDITING not in started:
        available.append(S.StageType.AUTHOR_EDITING)
    if first_done and not second_done and S.StageType.SECOND_VERIFICATION not in existing:
        available.append(S.StageType.SECOND_VERIFICATION)
    if second_done:
        available.append(S.StageType.EDITING_CONTROL)
    labels = dict(S.StageType.choices)
    return [(kind, labels[kind]) for kind in available]


class StageDatesForm(forms.Form):
    started_at = forms.DateField(
        label='Data rozpoczęcia', required=False,
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
    )
    ended_at = forms.DateField(
        label='Data zakończenia', required=False,
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
    )
    finish = forms.BooleanField(
        label='Zakończ etap i przekaż tekst dalej', required=False,
        help_text='Zapisuje zakończenie i uruchamia przejście workflow z podaną datą.',
    )
    next_stage = forms.ChoiceField(
        label='Przekaż redakcję do', required=False,
        choices=[('', 'Wybierz kolejny etap'), *[
            (kind, dict(S.StageType.choices)[kind]) for kind in EDITING_TRANSITIONS
        ]],
    )
    version = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, stage, **kwargs):
        super().__init__(*args, **kwargs)
        self.stage = stage
        if stage.stage_type == S.StageType.EDITING_CONTROL and not stage.is_completed:
            from workflow.decision_forms import editorial_decision_field
            self.fields['send_to_proofreading'] = editorial_decision_field(required=False)
        if (stage.is_completed or not stage.is_current or not stage.is_released
                or stage.workflow_cycle != stage.text.current_workflow_cycle
                or stage.stage_type not in {*STAGE_ROLES, S.StageType.READY_FOR_EDITING}):
            self.fields.pop('finish')
        if (stage.stage_type not in (S.StageType.EDITING, S.StageType.READY_FOR_EDITING)
                or stage.repetition_id or 'finish' not in self.fields):
            self.fields.pop('next_stage')
        else:
            self.fields['next_stage'].choices = [
                ('', 'Wybierz kolejny etap'), *editing_transition_choices(stage.text),
            ]

    def clean(self):
        data = super().clean()
        if data.get('finish'):
            if 'send_to_proofreading' in self.fields and data.get('send_to_proofreading') is None:
                self.add_error('send_to_proofreading', 'Wybierz dalszą redakcję albo pierwszą korektę.')
            for field in ('started_at', 'ended_at'):
                if not data.get(field):
                    self.add_error(field, 'Zakończenie etapu wymaga obu dat.')
            if 'next_stage' in self.fields and not data.get('next_stage'):
                self.add_error('next_stage', 'Wybierz kolejny etap redakcji.')
        elif not self.stage.is_completed and data.get('ended_at'):
            self.add_error('ended_at', 'Zaznacz „Zakończ etap i przekaż tekst dalej”.')
        return data


@transaction.atomic
@track_workflow
def set_stage_dates(stage_id, user, version, *, started_at=None, ended_at=None,
                    finish=False, next_stage=None, send_to_proofreading=None):
    if not user.is_active or not user.is_superuser:
        raise PermissionDenied
    text_id = S.objects.values_list('text_id', flat=True).get(pk=stage_id)
    text = Text.objects.select_for_update().get(pk=text_id)
    stage = S.objects.select_for_update().select_related('assignment__assigned_to').get(pk=stage_id)
    stage.text = text
    if version_of(text) != version:
        raise ValidationError('Workflow zmienił się. Odśwież formularz.')
    require_working_anthology(text)

    if stage.is_completed:
        if finish:
            raise ValidationError('Ten etap został już zakończony.')
        stage.started_at = started_at
        stage.ended_at = ended_at
        stage.full_clean()
        stage.save(update_fields=['started_at', 'ended_at'])
        return stage

    ensure_stage_belongs_to_current_cycle(stage)
    ensure_text_is_not_withdrawn(text)
    from workflow.repetitions import require_released
    require_released(stage)
    if ended_at and not finish:
        raise ValidationError('Aby zapisać zakończenie, zaznacz przekazanie tekstu do kolejnego etapu.')
    if finish and (not started_at or not ended_at):
        raise ValidationError('Zakończenie etapu wymaga obu dat.')

    if started_at != stage.started_at:
        if started_at:
            _validate_date(started_at)
            if started_at >= timezone.localdate():
                validate_assignment_start_date(started_at)
            if stage.started_at is None and stage.stage_type != S.StageType.AUTHOR_EDITING:
                from core.services.texts import start_assigned_stage
                start_assigned_stage(user=user, stage_id=stage.pk, started_at=started_at, allow_past=True)
                stage.refresh_from_db()
                stage.text = text
            else:
                from core.services.texts import _require_start_order, _require_eligible_assignee
                stages = list(S.objects.current_cycle().filter(text=text))
                _require_start_order(stage, stages)
                if not stage.assignment_id or not stage.assignment.assigned_to_id:
                    raise ValidationError('Najpierw przypisz osobę do tego etapu.')
                _require_eligible_assignee(stage.assignment.assigned_to, STAGE_ROLES.get(stage.stage_type), existing=True)
                latest = S.objects.current_cycle().filter(text=text, is_completed=True).aggregate(
                    latest=Max('ended_at'))['latest']
                if latest and started_at < latest:
                    raise ValidationError('Rozpoczęcie nie może poprzedzać zakończenia wcześniejszych prac.')
        stage.started_at = started_at
        stage.full_clean()
        stage.save(update_fields=['started_at'])

    if finish:
        if stage.stage_type == S.StageType.EDITING and not stage.repetition_id:
            transition = EDITING_TRANSITIONS.get(next_stage)
            if transition is None:
                raise ValidationError('Wybierz kolejny etap redakcji.')
            transition(text, user, ended_at)
        elif stage.stage_type == S.StageType.AUTHOR_EDITING and not stage.repetition_id:
            resume_editing(text, user, ended_at)
        else:
            complete_stage(stage, user, ended_at, send_to_proofreading=send_to_proofreading)
        stage.refresh_from_db()
    return stage
