"""Etykiety pracy 29 tekstów archiwalnych (dane z importu archiwum)."""

SOURCE = 'archive-table-2026-10-05-22'


def is_archive_text(text):
    return text.import_source == SOURCE and text.import_source_row in range(1, 30)


def archive_work_label(text, kind, number):
    """Use ordinary role names and execution numbering for historical work."""
    if not is_archive_text(text):
        return None
    from workflow.catalog import all_stage_roles
    from workflow.models import WorkflowRoleAssignment
    role = all_stage_roles().get(kind, kind)
    label = dict(WorkflowRoleAssignment.Role.choices).get(role)
    if label:
        return f'{label} (wyk. {number})' if number > 1 else label
    return None
