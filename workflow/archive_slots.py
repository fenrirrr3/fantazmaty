"""The agreed role allocation for the four historical anthologies only."""
from copy import deepcopy

from workflow.archive_executions import VERIFY, PROOF


def slotted_archive(data):
    result = deepcopy(data)
    proof_slots = ('first_proofreading', 'second_proofreading',
                   'third_proofreading', 'fourth_proofreading')
    verify_slots = ('first_verification', 'second_verification', 'third_verification')
    for row in result['texts']:
        verification = [s for s in row['stages'] if s['stage_type'] in VERIFY]
        editorial = [s for s in verification if s['source_column'].strip().rstrip(':') == 'Weryfikator redakcji']
        if len(editorial) > 1 or (editorial and verification[0] is not editorial[0]):
            raise ValueError(f"{row['title']}: nieoczekiwana kolejność weryfikatora redakcji.")
        proof_index = verify_index = 0
        for stage in row['stages']:
            if stage['stage_type'] in PROOF:
                stage['stage_type'] = proof_slots[min(proof_index, 3)]
                proof_index += 1
            elif stage['stage_type'] in VERIFY:
                # With an editorial verifier the first two people share V1.
                slot = max(0, verify_index - bool(editorial))
                stage['stage_type'] = verify_slots[min(slot, 2)]
                verify_index += 1
            person = stage['person']
            if (person['first_name'], person['last_name']) == ('Ilona', 'Skrzypczak'):
                person['last_name'] = 'Żurawska'
                person.pop('person_id', None)
    return result
