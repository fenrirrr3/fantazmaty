"""Import completed work into the ordinary workflow, without invented dates.

Existing workflow is never overwritten. The caller owns dataset/identity preparation;
this service imports into an existing Text and resolves existing accounts only.
"""
from datetime import date
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from texts.models import Text
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.catalog import all_stage_roles, active_stage_choices, IMPORT_ONLY_STAGE_TYPES, IMPORT_ONLY_ROLES


def parse_date(value):
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ValidationError('Daty muszą mieć format YYYY-MM-DD albo null.')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError('Nieprawidłowa data; wymagany format YYYY-MM-DD.') from exc


@transaction.atomic
def import_completed_workflow(*, text_id, stages, next_stage):
    if type(text_id) is not int or text_id < 1:
        raise ValidationError('text_id musi być dodatnią liczbą całkowitą.')
    text = Text.objects.select_for_update().get(pk=text_id)
    # Early editorial handoffs require their own interactive transitions. Import
    # can finish a text or open a normal late-entry checkpoint with no assignee.
    continuations = {S.StageType.READY, S.StageType.READY_FOR_EDITING,
        S.StageType.EDITING_CONTROL, S.StageType.FIRST_PROOFREADING,
        S.StageType.SECOND_PROOFREADING, S.StageType.THIRD_VERIFICATION,
        S.StageType.COORDINATOR_CONTROL, S.StageType.THIRD_PROOFREADING,
        S.StageType.FOURTH_PROOFREADING, S.StageType.STYLING}
    if next_stage not in continuations:
        raise ValidationError('Wskaż gotowy tekst (ready) albo obsługiwany etap dalszej pracy. Powroty redakcyjne wymagają osobnego przygotowania danych.')
    if text.anthology_id and text.anthology.status == 'ready' and next_stage != S.StageType.READY:
        raise ValidationError('Tekst w gotowej antologii nie może oczekiwać na dalszą pracę.')
    if not isinstance(stages, list) or not stages:
        raise ValidationError('stages musi być niepustą listą zakończonych etapów.')
    stage_roles = all_stage_roles()
    expected = []
    people = {}
    seen = set()
    for row in stages:
        if not isinstance(row, dict) or set(row) - {'stage_type', 'assigned_to', 'started_at', 'ended_at'} or 'stage_type' not in row:
            raise ValidationError('Nieprawidłowe pola etapu.')
        kind = row['stage_type']
        if kind not in stage_roles or kind == next_stage:
            raise ValidationError('Nieznany lub powtórzony etap. Powtórzenia rzeczywistej pracy obsługuje osobna operacja workflow.')
        order = [value for value, _ in active_stage_choices()]
        if kind not in IMPORT_ONLY_STAGE_TYPES and order.index(kind) >= order.index(next_stage):
            raise ValidationError('Dalszy etap musi następować po wszystkich importowanych etapach; cofanie wymaga operacji powtórzenia.')
        email = row.get('assigned_to')
        if not isinstance(email, str) or not email.strip():
            raise ValidationError('Zakończony etap wymaga adresu konta wykonawcy w assigned_to.')
        users = list(get_user_model().objects.filter(email__iexact=email.strip())[:2])
        if len(users) != 1:
            raise ValidationError(f'Adres {email} musi wskazywać dokładnie jedno istniejące konto wykonawcy.')
        person = users[0]
        role = stage_roles[kind]
        if (kind,person.pk) in seen:
            raise ValidationError('Powtórzony etap tej samej osoby w pliku.')
        seen.add((kind,person.pk))
        people[(role,person.pk)] = person
        expected.append((kind, person.pk, parse_date(row.get('started_at')), parse_date(row.get('ended_at'))))
    actual = list(S.objects.filter(text=text).select_related('assignment').order_by('pk'))
    if actual:
        imported = [(s.stage_type, s.assignment.assigned_to_id if s.assignment else None, s.started_at, s.ended_at)
                    for s in actual if s.imported_completed and s.stage_type != S.StageType.READY]
        other = [s for s in actual if s.stage_type == next_stage]
        # A second identical import is a no-op; never reset work begun after import.
        if (len(actual) == len(expected) + 1 and imported == expected and len(other) == 1
                and all(s.is_current == (s.stage_type not in IMPORT_ONLY_STAGE_TYPES)
                        and s.is_released == (s.stage_type not in IMPORT_ONLY_STAGE_TYPES)
                        and s.workflow_cycle == text.current_workflow_cycle for s in actual)
                and other[0].started_at is None and other[0].ended_at is None
                and other[0].assignment_id is None
                and not other[0].is_completed and not other[0].imported_completed
                and not A.objects.filter(text=text, assigned_at__isnull=False).exists()):
            return False
        raise ValidationError('Tekst ma już workflow lub dane zmieniły się po imporcie. Niczego nie nadpisano.')
    if A.objects.filter(text=text).exists():
        raise ValidationError('Tekst ma już przypisania; import nie może ich nadpisać.')
    token = importing_completed.set(True)
    try:
        assignments = {}
        role_numbers = {}
        for (role, user_id), user in people.items():
            role_numbers[role] = role_numbers.get(role,0) + 1
            A.objects.filter(text=text,workflow_cycle=text.current_workflow_cycle,role=role,is_current=True).update(is_current=False)
            a = A(text=text, workflow_cycle=text.current_workflow_cycle, role=role, assigned_to=user, execution_number=role_numbers[role], is_current=role not in IMPORT_ONLY_ROLES)
            # Earlier imported V1 and V2 work can have the same performer.
            # Keep the earlier assignment in history, never as a conflicting live role.
            if role in (A.Role.VERIFIER_1, A.Role.VERIFIER_2):
                A.objects.filter(text=text, workflow_cycle=text.current_workflow_cycle,
                    role__in=(A.Role.VERIFIER_1, A.Role.VERIFIER_2), assigned_to=user,
                    is_current=True).update(is_current=False)
            a.full_clean(); a.save()
            # Date of import is not the date when a person took the work.
            A.objects.filter(pk=a.pk).update(assigned_at=None)
            assignments[(role,user.pk)] = a
        iterations = {}
        for kind, user_id, started, ended in expected:
            iterations[kind] = iterations.get(kind,0) + 1
            stage = S(text=text, workflow_cycle=text.current_workflow_cycle, stage_type=kind,
                      assignment=assignments[(stage_roles[kind],user_id)], iteration=iterations[kind],
                      execution_number=iterations[kind],
                      is_current=kind not in IMPORT_ONLY_STAGE_TYPES, is_released=kind not in IMPORT_ONLY_STAGE_TYPES,
                      started_at=started, ended_at=ended,
                      is_completed=True, imported_completed=True)
            stage.full_clean(); stage.save()
        # READY is a terminal status marker, not a performed task. Preserve
        # ordinary workflow semantics, with no fictional start/end dates.
        marker = S(text=text, workflow_cycle=text.current_workflow_cycle, stage_type=next_stage)
        marker.full_clean(); marker.save()
    finally:
        importing_completed.reset(token)
    return True
