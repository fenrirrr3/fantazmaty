"""Correct performers in place; never start work or increment execution numbers."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.apps import apps
from core.edit_versions import version_of, bump
from texts.models import Text
from workflow.catalog import all_stage_roles, IMPORT_ONLY_ROLES
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A, WorkflowHandoff
from workflow.assignment_merge import role_group, write_group


def require_eligible_correction(performer, role):
    """Admin corrects identity independently of the person's current team roles.

    Live work still requires an available active account. Completed historical
    work does not call this check. Normal claims retain their role policy.
    """
    from people.leave_access import require_available
    from core.permissions import get_active_person_profile
    require_available(performer, lock=True)
    if not performer or not performer.is_active or (
            not performer.is_superuser and get_active_person_profile(performer) is None):
        raise ValidationError('Bieżąca praca wymaga aktywnego konta powiązanego z aktywną osobą w zespole.')
    if role == A.Role.STYLING and not performer.is_superuser:
        raise ValidationError('Stylowanie można przypisać tylko superuserowi.')



def validate_proofreader_plan(text, plan):
    """Check the final batch, including historical first work and unchanged roles."""
    proof_roles = {A.Role.PROOFREADER_1, A.Role.PROOFREADER_2, A.Role.PROOFREADER_3}
    if not any(item['assignment'] and item['assignment'].role in proof_roles for item in plan):
        return
    assignments = list(A.objects.filter(text=text, role__in=proof_roles).prefetch_related('stages'))
    overrides = {item['assignment'].pk: item for item in plan if item['assignment'] and item['assignment'].pk}
    handed_off = set(WorkflowHandoff.objects.filter(text=text).values_list('previous_assignment_id', flat=True))
    from django.utils import timezone
    today = timezone.localdate()
    first_users, later_users = set(), set()
    merged_ids = {pk for item in plan for pk in item.get('merge_ids', ())}
    pending = [(a, overrides.get(a.pk)) for a in assignments if a.pk not in merged_ids]
    pending.extend((item['assignment'], item) for item in plan
                   if item['assignment'] and item['assignment'].pk is None and item['assignment'].role in proof_roles)
    for assignment, item in pending:
        user_id = (item['performer'].pk if item['performer'] else None) if item else assignment.assigned_to_id
        if user_id is None:
            continue
        stages = {stage.pk: stage for stage in assignment.stages.all()} if assignment.pk else {}
        if item:
            stages.update({stage.pk: stage for stage in item.get('stages', ())})
        if assignment.role == A.Role.PROOFREADER_1:
            if assignment.pk in handed_off or any(stage.is_completed or
                    (stage.started_at is not None and stage.started_at <= today) for stage in stages.values()):
                first_users.add(user_id)
        elif any(stage.is_current and stage.workflow_cycle == text.current_workflow_cycle and
                 not stage.is_completed for stage in stages.values()):
            later_users.add(user_id)
    if first_users & later_users:
        raise ValidationError('Osoba wykonująca pierwszą korektę nie może wykonywać drugiej ani trzeciej korekty tego tekstu. Sprawdź wszystkie zmieniane przydziały.')



def performer_plan(text, changes):
    """Validate the whole form before saving anything; caller holds the Text lock."""
    from workflow.anthology_policy import require_working_anthology
    if changes:
        require_working_anthology(text)
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
        merge_plan = None
        if stage.repetition_id is None:
            merge_plan, _ = role_group(apps, text._state.db or 'default', text,
                                       stage.workflow_cycle, role)
            if merge_plan:
                assignment = merge_plan['assignment']
                assignment.is_current = stage.workflow_cycle == text.current_workflow_cycle
        if assignment is None and stage.is_current:
            assignment = A.objects.filter(text=text, workflow_cycle=stage.workflow_cycle, role=role, is_current=True).first()
        if assignment and (assignment.role != role or assignment.workflow_cycle != stage.workflow_cycle or assignment.text_id != text.pk):
            raise ValidationError('Przypisanie nie odpowiada etapowi. Najpierw popraw powiązanie w danych.')
        key = ('assignment', assignment.pk) if assignment else ('role', stage.workflow_cycle, role, stage.is_current)
        if key in grouped:
            if grouped[key]['performer'] != performer:
                raise ValidationError('Etapy korzystają z jednego przypisania. Wybierz tę samą osobę albo zmień ją tylko w jednym wierszu.')
            if stage.pk not in {item.pk for item in grouped[key]['stages']}:
                grouped[key]['stages'].append(stage)
        else:
            grouped[key] = dict(assignment=assignment, performer=performer,
                                stages=merge_plan['stages'] if merge_plan else [stage], role=role,
                                merge_plan=merge_plan,
                                merge_ids=merge_plan['merge_ids'] if merge_plan else set())
    for item in grouped.values():
        assignment, performer = item['assignment'], item['performer']
        related = item['stages'] if item['merge_plan'] else (list(assignment.stages.all()) if assignment else item['stages'])
        if assignment and assignment.assigned_to_id == (performer.pk if performer else None):
            continue
        if assignment and WorkflowHandoff.objects.filter(Q(previous_assignment=assignment) | Q(new_assignment=assignment)).exists():
            raise ValidationError('To przypisanie ma zarejestrowane przekazanie pracy. Korekta nie może nadpisać jego uczestników.')
        if performer is None:
            if any(s.started_at or s.ended_at or s.is_completed for s in related):
                raise ValidationError('Praca ma już daty lub jest zakończona. Wybierz innego wykonawcę zamiast pozostawiać puste pole.')
        elif any(s.is_current and not s.is_completed for s in related):
            require_eligible_correction(performer, item['role'])
        if assignment is None and performer is not None:
            stage = item['stages'][0]
            number = (A.objects.filter(text=text, workflow_cycle=stage.workflow_cycle, role=item['role']).aggregate(n=Max('execution_number'))['n'] or 0) + 1
            assignment = A(text=text, workflow_cycle=stage.workflow_cycle, role=item['role'],
                           execution_number=number, is_current=(stage.is_current if stage.repetition_id else stage.workflow_cycle == text.current_workflow_cycle) and item['role'] not in IMPORT_ONLY_ROLES,
                           repetition=stage.repetition)
            item['assignment'] = assignment
        if assignment:
            assignment.assigned_to = performer
            token = importing_completed.set(True)
            was_current = assignment.is_current
            try:
                # The final current-role and verifier constraints are checked below.
                assignment.is_current = False
                assignment.full_clean()
            finally:
                assignment.is_current = was_current
                importing_completed.reset(token)
    # Validate two initially empty verification roles together, before either is saved.
    effective = {a.pk: (a.workflow_cycle, a.role, a.assigned_to_id)
                 for a in A.objects.filter(text=text, is_current=True, role__in=('verifier_1','verifier_2'))}
    for item in grouped.values():
        for pk in item['merge_ids']:
            effective.pop(pk, None)
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
    plan = list(grouped.values())
    validate_proofreader_plan(text, plan)
    return plan


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
    from workflow.waiting import performer_snapshot, reset_changed_performers
    before_performers = performer_snapshot(text)
    token = importing_completed.set(True)
    try:
        # Free both verifier slots before a swap; all writes stay in this transaction.
        verifier_ids = [item['assignment'].pk for item in plan
                        if item['assignment'] and item['assignment'].pk
                        and item['assignment'].role in ('verifier_1', 'verifier_2')]
        verifier_ids.extend(pk for item in plan if item['role'] in ('verifier_1', 'verifier_2')
                            for pk in item['merge_ids'])
        A.objects.filter(pk__in=verifier_ids).update(assigned_to=None)
        for item in plan:
            assignment, performer = item['assignment'], item['performer']
            if assignment is None:
                continue
            if item['merge_plan']:
                write_group(apps, text._state.db or 'default', item['merge_plan'],
                            user_id=performer.pk if performer else None,
                            is_current=assignment.is_current)
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
            reset_changed_performers(text, before_performers)
            bump('texts.text',text.pk,'default')
    finally:
        importing_completed.reset(token)
