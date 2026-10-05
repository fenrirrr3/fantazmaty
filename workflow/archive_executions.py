"""Explicit normalization of the 29 historical texts, not live workflow."""
from copy import deepcopy

SOURCE = 'archive-table-2026-10-05-22'
VERIFY = {'first_verification', 'second_verification', 'third_verification',
          'fourth_verification', 'fifth_verification', 'sixth_verification', 'seventh_verification'}
PROOF = {'first_proofreading', 'second_proofreading', 'third_proofreading', 'fourth_proofreading'}


def normalized_archive(data):
    result = deepcopy(data)
    for row in result['texts']:
        for stage in row['stages']:
            if stage['stage_type'] in VERIFY:
                stage['stage_type'] = 'first_verification'
            elif stage['stage_type'] in PROOF:
                stage['stage_type'] = 'first_proofreading'
            person = stage['person']
            if (person['first_name'], person['last_name']) == ('Ilona', 'Skrzypczak'):
                person['last_name'] = 'Żurawska'
                person.pop('person_id', None)
    return result


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
