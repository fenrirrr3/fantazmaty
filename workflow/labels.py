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
    return f'{label} (wyk. {number})' if show_first or number > 1 else label


def assignment_label(assignment, *, show_first=False):
    label = assignment.get_role_display()
    if show_first or assignment.execution_number > 1:
        label = WORK_LABELS.get(assignment.role, label)
    return execution_label(label, assignment.execution_number, show_first=show_first)
