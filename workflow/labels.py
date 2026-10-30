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


def execution_label(label, number):
    return f'{label} (wyk. {number})' if number > 1 else label


def assignment_number(assignment):
    """Numer wykonania do pokazania: przy dodatkowym wykonaniu – numer jego etapu (wyk. 2, 3…), nie przydziału."""
    if getattr(assignment, 'is_additional', False) and assignment.pk:
        stage = next(iter(assignment.stages.all()), None)
        if stage is not None:
            return stage.execution_number
    return assignment.execution_number


def assignment_label(assignment):
    from workflow.archive_executions import archive_work_label
    number = assignment_number(assignment)
    special = archive_work_label(assignment.text, assignment.role, number) if assignment.text_id else None
    if special:
        return special
    # An execution number describes the assignment's history, not a new role.
    return execution_label(assignment.get_role_display(), number)


def stage_label(stage, text=None):
    from workflow.archive_executions import archive_work_label
    special = archive_work_label(text or stage.text, stage.stage_type, stage.execution_number) if stage.imported_completed else None
    return special or execution_label(stage.get_stage_type_display(), stage.execution_number)
