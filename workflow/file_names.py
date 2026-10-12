"""Nazwa pliku na Dropboksie dla bieżącego etapu – zgodnie z wytycznymi „Nazewnictwo plików”.

Wzór: oznaczenie pliku + nazwisko autora – tytuł, np. „REDAKCJA R POCZ Kowalski – Tytuł”.
Oznaczenie to plik, który osoba dodaje na Dropboksa, kończąc dany etap (na etapie „Do redakcji” – ORYGINAŁ).
"""
from workflow.models import WorkflowStage

StageType = WorkflowStage.StageType

FILE_LABELS = {
    StageType.READY_FOR_EDITING: 'ORYGINAŁ',
    StageType.FIRST_VERIFICATION: 'REDAKCJA W1',
    StageType.SECOND_VERIFICATION: 'REDAKCJA W2',
    StageType.EDITING_CONTROL: '1 KOREKTA POCZ',
    StageType.FIRST_PROOFREADING: '2 KOREKTA POCZ',
    StageType.SECOND_PROOFREADING: 'W3 POCZ',
    StageType.THIRD_VERIFICATION: 'W3 ZAK',
    StageType.COORDINATOR_CONTROL: 'WERYFIKACJA R POCZ',
    StageType.EDITOR_CONTROL: 'WERYFIKACJA R ZAK',
    StageType.THIRD_PROOFREADING: '3 KOREKTA ZAK',
    StageType.FOURTH_PROOFREADING: 'DO STYLOWANIA',
    StageType.STYLING: 'GOTOWE',
    StageType.READY: 'GOTOWE',
}
EDITING = {StageType.EDITING, StageType.AUTHOR_EDITING}


def _editing_label(stages):
    """Redakcja: przed W1 – R POCZ, po W1 – R ZAK, po W2 albo po kontroli koordynatora – REDAKCJA ZAK."""
    done = {stage.stage_type for stage in stages if stage.is_completed}
    if done & {StageType.SECOND_VERIFICATION, StageType.EDITING_CONTROL}:
        return 'REDAKCJA ZAK'
    if StageType.FIRST_VERIFICATION in done:
        return 'REDAKCJA R ZAK'
    return 'REDAKCJA R POCZ'


def file_label(stage, stages):
    if stage is None:
        return None
    if stage.stage_type in EDITING:
        return _editing_label(stages)
    return FILE_LABELS.get(stage.stage_type)


def author_surnames(authors):
    """Nazwiska autorów w kolejności podpisu; autor podpisany pseudonimem – pseudonim."""
    def get(author, key):
        return (author.get(key) if isinstance(author, dict) else getattr(author, key, '')) or ''

    names = []
    for author in authors:
        last = get(author, 'last_name').strip()
        full = f"{get(author, 'first_name')} {last}".strip()
        shown = get(author, 'display_name').strip()
        name = shown if shown and shown != full else last or shown
        if name and name not in names:
            names.append(name)
    return ', '.join(names)


def stage_file_name(text_title, authors, stage, stages):
    """Pełna nazwa pliku dla etapu albo None, gdy etap nie ma własnego pliku (np. tekst wycofany)."""
    label = file_label(stage, stages)
    if not label:
        return None
    surnames = author_surnames(authors)
    return f'{label} {surnames} – {text_title}' if surnames else f'{label} {text_title}'
