"""Imported work labels are separate from operational roles and stage choices."""

IMPORT_ONLY_STAGE_ROLES = {
    "editing_review": "editing_reviewer",
    "fourth_verification": "verifier_4",
    "fifth_verification": "verifier_5",
    "sixth_verification": "verifier_6",
    "seventh_verification": "verifier_7",
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


# Ekstrakty: jeden wspólny tekst tomu przechodzi tylko skróconą ścieżkę.
EXTRACTS_STAGE_TYPES = ('ready_for_editing', 'editing', 'first_verification', 'first_proofreading', 'ready',
                        'withdrawn')
# Kolejne etapy pracy po redakcji; „Gotowe” zamyka ścieżkę.
EXTRACTS_FLOW = ('first_verification', 'first_proofreading', 'ready')


def is_extracts_text(text):
    """Tekst należy do tomu Ekstraktów (flaga antologii lub tom z importu)."""
    if not getattr(text, 'anthology_id', None):
        return False
    from texts.extract_whole import is_extract_anthology
    return is_extract_anthology(text.anthology)


def extracts_text_q(prefix=''):
    """Warunek zapytania na teksty Ekstraktów; prefix wskazuje relację do tekstu, np. 'text__'."""
    from django.db.models import Q
    return (Q(**{f'{prefix}anthology__is_extracts': True})
            | Q(**{f'{prefix}anthology__extract_volume__isnull': False}))


def stage_allowed(text, kind):
    return kind in EXTRACTS_STAGE_TYPES or not is_extracts_text(text)


def disabled_stage_message(kind):
    from workflow.models import WorkflowStage
    label = dict(WorkflowStage.StageType.choices).get(kind, kind)
    return (f'Etap „{label}” jest wyłączony w Ekstraktach. Ich workflow to: Do redakcji → Redakcja → '
            'Pierwsza weryfikacja → Pierwsza korekta → Gotowe.')


def require_stage_allowed(text, kind):
    from django.core.exceptions import ValidationError
    if not stage_allowed(text, kind):
        raise ValidationError(disabled_stage_message(kind))


def stage_choices_for(text, choices):
    """Zawęża wybory etapów do dozwolonych dla tekstu (Ekstrakty – tylko skrócona ścieżka)."""
    if text is None or not is_extracts_text(text):
        return list(choices)
    return [(kind, label) for kind, label in choices if kind in EXTRACTS_STAGE_TYPES]


def extracts_successor(kind, done=()):
    """Najbliższy dozwolony etap po zakończeniu `kind` w Ekstraktach.

    Kolejność jak w katalogu etapów; „Plik u autora” należy do redakcji, więc prowadzi
    do pierwszej weryfikacji. Pomija etapy z `done` (już zakończone), z wyjątkiem „Gotowe”.
    """
    order = [value for value, _ in active_stage_choices()]
    position = order.index('editing' if kind == 'author_editing' else kind)
    for target in EXTRACTS_FLOW:
        if order.index(target) > position and (target == 'ready' or target not in done):
            return target
    return None


def extracts_next_stage(text, kind):
    """Jak extracts_successor, z pominięciem etapów zakończonych w bieżącym przebiegu."""
    from workflow.services import completed_stage_exists
    done = {target for target in EXTRACTS_FLOW[:-1] if completed_stage_exists(text, target)}
    return extracts_successor(kind, done)
