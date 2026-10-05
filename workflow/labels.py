"""Short execution labels used in the CMS; model choices remain unchanged."""
WORK_LABELS = {
    'editor': 'Redakcja', 'editing_coordinator': 'Kontrola K. redakcji',
    'proofreader_1': 'Pierwsza korekta', 'proofreader_2': 'Druga korekta',
    'proofreader_3': 'Trzecia korekta', 'proofreader_4': 'Czwarta korekta',
    'verifier_1': 'Pierwsza weryfikacja', 'verifier_2': 'Druga weryfikacja',
    'verifier_3': 'Trzecia weryfikacja', 'verifier_4': 'Czwarta weryfikacja',
    'verification_coordinator': 'Kontrola K. weryfikacji',
    'editing_reviewer': 'Kontrola redakcji', 'styling': 'Stylowanie',
}


def execution_label(label, number, *, show_first=False):
    return f'{label} (wyk. {number})' if number > 1 else label


def assignment_label(assignment, *, show_first=False):
    from workflow.archive_executions import archive_work_label
    special = archive_work_label(assignment.text, assignment.role, assignment.execution_number) if assignment.text_id else None
    if special:
        return special
    # An execution number describes the assignment's history, not a new role.
    return execution_label(assignment.get_role_display(), assignment.execution_number,
                           show_first=show_first)


def stage_label(stage, text=None):
    from workflow.archive_executions import archive_work_label
    special = archive_work_label(text or stage.text, stage.stage_type, stage.execution_number) if stage.imported_completed else None
    return special or execution_label(stage.get_stage_type_display(), stage.execution_number)
