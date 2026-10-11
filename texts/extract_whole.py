"""One workflow per volume; source miniatures remain in the Extract register."""
from django.core.exceptions import ValidationError
from django.db import transaction
from texts.models import Anthology, Text
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage

SOURCE = 'extract-volume-v2'


def is_extract_anthology(book):
    # Explicit admin flag; imported volumes are always flagged as well.
    return bool(book.is_extracts) or hasattr(book, 'extract_volume')


def published_legacy_volume(book):
    marker = getattr(book, 'extract_volume', None)
    name = ' '.join(book.title.casefold().split())
    return bool(marker and marker.number in (1, 2)) or name in {
        'ekstrakty', 'ekstrakty 1', 'ekstrakty i', 'ekstrakty 2', 'ekstrakty ii'}


@transaction.atomic
def ensure_whole_text(book):
    # Serialize admin edits, command reruns and anthology saves on the same parent.
    book = Anthology.objects.select_for_update().get(pk=book.pk)
    if not is_extract_anthology(book):
        return None
    if book.is_novel:
        raise ValidationError('Ekstrakty są antologią zbiorczą, bez trybu powieści.')
    whole = Text.objects.filter(import_source=SOURCE, import_source_row=book.pk).first()
    if whole:
        if whole.anthology_id != book.pk:
            raise ValidationError('Tekst całych Ekstraktów został przeniesiony do innej antologii.')
        if whole.title != book.title:
            whole.title = book.title
            whole.save(update_fields=['title'])
        return whole
    if book.texts.exists():
        # Existing installations are converted explicitly with a report first.
        return None
    ready = book.status == 'ready' or published_legacy_volume(book)
    whole = Text.objects.create(title=book.title, length=None, import_source=SOURCE,
                                import_source_row=book.pk)
    token = importing_completed.set(True)
    try:
        WorkflowStage.objects.create(text=whole, stage_type='ready' if ready else 'ready_for_editing',
                                     is_current=True, is_released=True)
    finally:
        importing_completed.reset(token)
    whole.anthology = book
    whole.save(update_fields=['anthology'])
    if ready and book.status != 'ready':
        book.status = 'ready'
        book.save(update_fields=['status'])
    return whole
