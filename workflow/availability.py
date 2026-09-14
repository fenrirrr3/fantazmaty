"""One claim policy shared by displayed availability and the mutation service."""
from core.permissions import is_team_member, is_coordinator, has_role
from workflow.models import WorkflowRoleAssignment as A

def claim_reason(stage, user, stages, assignments):
    from workflow.services import STAGE_ROLES, ROLE_GROUPS
    kind=stage.stage_type
    role=A.Role.EDITOR if kind=='ready_for_editing' else STAGE_ROLES.get(kind)
    if not is_team_member(user):return 'Brak aktywnego dostępu do zespołu.'
    if any(s.stage_type in ('ready','withdrawn') for s in stages):return 'Proces jest zamknięty.'
    if stage.is_completed or stage.started_at or stage.ended_at:return 'Etap nie oczekuje na przejęcie.'
    if not role:return 'Tego etapu nie można przejąć.'
    if kind=='styling' and not user.is_superuser:return 'Stylowanie jest dostępne tylko dla superusera.'
    if not (is_coordinator(user) or has_role(user,ROLE_GROUPS.get(role,'Redaktor'))):return 'Brak wymaganej roli.'
    occupied={a.role:a.assigned_to_id for a in assignments if a.assigned_to_id}
    if role in occupied:return 'Rola ma już przypisanego wykonawcę.'
    opposite={'verifier_1':'verifier_2','verifier_2':'verifier_1'}.get(role)
    if opposite and occupied.get(opposite)==user.pk:return 'Pierwszą i drugą weryfikację wykonują różne osoby.'
    entry=len(stages)==1 and not stage.is_completed
    if kind=='editor_control':return 'Kontrolę rozpoczyna przypisany redaktor.'
    if kind in ('editing','author_editing') and not entry:return 'Użyj przekazania lub wznowienia redakcji.'
    if kind=='first_verification' and not entry and not any(s.stage_type=='editing' and not s.is_completed and s.started_at and not s.ended_at for s in stages):return 'Rezerwacja wymaga rozpoczętej redakcji.'
    if kind=='second_verification' and not entry:
        if not any(s.stage_type=='first_verification' and s.is_completed for s in stages):return 'Najpierw zakończ pierwszą weryfikację.'
        if any(s.stage_type in ('editing','author_editing') and not s.is_completed and not s.ended_at for s in stages):return 'Redaktor musi przekazać tekst do drugiej weryfikacji.'
    return ''
