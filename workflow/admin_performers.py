"""Correct performers in place; never start work or increment execution numbers."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from core.edit_versions import version_of, bump
from texts.models import Text
from workflow.catalog import all_stage_roles, IMPORT_ONLY_ROLES
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowHandoff


def performer_plan(text, changes):
    """Validate the whole form before saving anything; caller holds the Text lock."""
    stages = {s.pk: s for s in S.objects.filter(text=text, pk__in=changes).select_related('assignment')}
    if set(stages) != set(changes):
        raise ValidationError('Lista etapów zmieniła się. Odśwież stronę.')
    grouped = {}
    roles = all_stage_roles()
    for pk, performer in changes.items():
        stage = stages[pk]
        role = roles.get(stage.stage_type)
        if not role:
            raise ValidationError('Status Gotowy, Wycofany lub Do redakcji nie ma wykonawcy.')
        assignment = stage.assignment
        if assignment is None and stage.is_current:
            assignment = A.objects.filter(text=text, workflow_cycle=stage.workflow_cycle, role=role, is_current=True).first()
        if assignment and (assignment.role != role or assignment.workflow_cycle != stage.workflow_cycle or assignment.text_id != text.pk):
            raise ValidationError('Przypisanie nie odpowiada etapowi. Najpierw popraw powiązanie w danych.')
        key = ('assignment', assignment.pk) if assignment else ('role', stage.workflow_cycle, role, stage.is_current)
        if key in grouped:
            if grouped[key]['performer'] != performer:
                raise ValidationError('Etapy korzystają z jednego przypisania. Wybierz tę samą osobę albo zmień ją tylko w jednym wierszu.')
            grouped[key]['stages'].append(stage)
        else:
            grouped[key] = dict(assignment=assignment, performer=performer, stages=[stage], role=role)
    for item in grouped.values():
        assignment, performer = item['assignment'], item['performer']
        related = list(assignment.stages.all()) if assignment else item['stages']
        if assignment and assignment.assigned_to_id == (performer.pk if performer else None):
            continue
        if assignment and WorkflowHandoff.objects.filter(Q(previous_assignment=assignment) | Q(new_assignment=assignment)).exists():
            raise ValidationError('To przypisanie ma zarejestrowane przekazanie pracy. Korekta nie może nadpisać jego uczestników.')
        if performer is None:
            if any(s.started_at or s.ended_at or s.is_completed for s in related):
                raise ValidationError('Praca ma już daty lub jest zakończona. Wybierz innego wykonawcę zamiast pozostawiać puste pole.')
        elif any(s.is_current and not s.is_completed for s in related):
            from core.services.texts import _require_eligible_assignee
            _require_eligible_assignee(performer, item['role'])
        if assignment is None and performer is not None:
            stage = item['stages'][0]
            number = (A.objects.filter(text=text, workflow_cycle=stage.workflow_cycle, role=item['role']).aggregate(n=Max('execution_number'))['n'] or 0) + 1
            assignment = A(text=text, workflow_cycle=stage.workflow_cycle, role=item['role'],
                           execution_number=number, is_current=stage.is_current and item['role'] not in IMPORT_ONLY_ROLES,
                           repetition=stage.repetition)
            item['assignment'] = assignment
        if assignment:
            assignment.assigned_to = performer
            token = importing_completed.set(True)
            try:
                assignment.full_clean()
            finally:
                importing_completed.reset(token)
    # Validate two initially empty verification roles together, before either is saved.
    effective = {a.pk: (a.workflow_cycle, a.role, a.assigned_to_id)
                 for a in A.objects.filter(text=text, is_current=True, role__in=('verifier_1','verifier_2'))}
    for key, item in grouped.items():
        assignment = item['assignment']
        if assignment and assignment.is_current and assignment.role in ('verifier_1','verifier_2'):
            effective[assignment.pk or key] = (assignment.workflow_cycle, assignment.role, assignment.assigned_to_id)
    seen = set()
    for cycle, role, user_id in effective.values():
        if user_id and (cycle, user_id) in seen:
            raise ValidationError('Pierwszą i drugą weryfikację muszą wykonywać różne osoby.')
        if user_id:
            seen.add((cycle, user_id))
    return list(grouped.values())


@transaction.atomic
def correct_stage_performers(text_id, changes, actor, expected_version):
    if not actor.is_active or not actor.is_superuser:
        raise PermissionDenied
    text = Text.objects.select_for_update().get(pk=text_id)
    if version_of(text) != expected_version:
        raise ValidationError('Dane zmieniły się. Odśwież formularz.')
    try:
        plan = performer_plan(text, changes)
    except ValidationError as exc:
        raise ValidationError(exc.messages) from exc
    token = importing_completed.set(True)
    try:
        for item in plan:
            assignment, performer = item['assignment'], item['performer']
            if assignment is None:
                continue
            if assignment.pk is None:
                assignment.save()
                A.objects.filter(pk=assignment.pk).update(assigned_at=None)
            else:
                A.objects.filter(pk=assignment.pk).update(assigned_to=performer)
                bump('workflow.workflowroleassignment',assignment.pk,'default')
            for stage in item['stages']:
                if stage.assignment_id != assignment.pk:
                    S.objects.filter(pk=stage.pk).update(assignment=assignment)
                    bump('workflow.workflowstage',stage.pk,'default')
        if plan:
            bump('texts.text',text.pk,'default')
    finally:
        importing_completed.reset(token)
