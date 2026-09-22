"""Imported work labels are separate from operational roles and stage choices."""

IMPORT_ONLY_STAGE_ROLES = {
    "editing_review": "editing_reviewer",
    "fourth_verification": "verifier_4",
}
IMPORT_ONLY_STAGE_TYPES = tuple(IMPORT_ONLY_STAGE_ROLES)
IMPORT_ONLY_ROLES = tuple(IMPORT_ONLY_STAGE_ROLES.values())


def active_stage_choices():
    from workflow.models import WorkflowStage
    return [(value, label) for value, label in WorkflowStage.StageType.choices
            if value not in IMPORT_ONLY_STAGE_TYPES]


def active_role_choices():
    from workflow.models import WorkflowRoleAssignment
    return [(value, label) for value, label in WorkflowRoleAssignment.Role.choices
            if value not in IMPORT_ONLY_ROLES]


def all_stage_roles():
    """For import and recorded history only; services keep their live mapping."""
    from workflow.services import STAGE_ROLES
    return {**STAGE_ROLES, **IMPORT_ONLY_STAGE_ROLES}


def workflow_role_choices():
    """Each operational role once, at its first position in the workflow."""
    from workflow.services import RESTARTABLE_STAGE_TYPES, STAGE_ROLES
    labels = dict(active_role_choices())
    ordered = dict.fromkeys(STAGE_ROLES[kind] for kind in RESTARTABLE_STAGE_TYPES if kind in STAGE_ROLES)
    return [(role, labels[role]) for role in ordered if role in labels]
