"""One workflow per volume; source miniatures remain in the Extract register."""
from django.core import serializers
from django.core.exceptions import ValidationError
from django.db import transaction
from texts.models import Anthology, ExtractTextLink, Text
from workflow.import_context import importing_completed
from workflow.models import WorkflowStage

SOURCE = 'extract-volume-v2'


def is_extract_anthology(book):
    return 'ekstrakty' in book.title.casefold() or hasattr(book, 'extract_volume')


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


def _legacy_snapshot(text):
    if text.import_source != 'extracts-v1' or not ExtractTextLink.objects.filter(text=text).exists():
        raise ValidationError(f'{text.title} (#{text.pk}): nie jest miniaturą z wcześniejszego importu.')
    if text.genre or text.tags or text.file_url or text.content_warnings or text.coordinator_note or text.length is not None or text.audiobook_blacklisted or not text.for_recording:
        raise ValidationError(f'{text.title} (#{text.pk}): uzupełnione dane miniatury wymagają osobnego rozstrzygnięcia.')
    stages = list(text.workflow_stages.all())
    if len(stages) != 1 or any(s.stage_type not in ('ready', 'ready_for_editing') or s.assignment_id
                             or s.started_at or s.ended_at or s.is_completed or s.workflow_cycle != 1 or s.repetition_id or s.waiting_reset_at or s.send_to_proofreading is not None
                             for s in stages):
        raise ValidationError(f'{text.title} (#{text.pk}): istnieje historia pracy; nie usuwam jej automatycznie.')
    # Refuse every additional relation, including reviews, actual assignments,
    # notes, audiobook/illustration data and future models unknown to this command.
    for relation in Text._meta.related_objects:
        if relation.related_model in (WorkflowStage, ExtractTextLink):
            continue
        if relation.related_model._default_manager.filter(**{relation.field.name: text}).exists():
            raise ValidationError(f'{text.title} (#{text.pk}): powiązane dane {relation.related_model._meta.label}; konwersja przerwana.')
    link = ExtractTextLink.objects.select_related('extract').get(text=text)
    if set(text.authors.values_list('pk', flat=True)) != {link.extract.author_id}:
        raise ValidationError(f'{text.title}: zmieniono autorów względem źródłowej miniatury.')
    from texts.extract_data import split_list
    from texts.services import normalize_author_name
    if not any(normalize_author_name(title) == normalize_author_name(link.source_title)
               for title in split_list(link.extract.accepted_titles)):
        raise ValidationError(f'{text.title}: źródłowa miniatura nie jest już na liście przyjętych.')
    return serializers.serialize('json', [text, *stages, link])


@transaction.atomic
def consolidate_volume(book):
    book = Anthology.objects.select_for_update().get(pk=book.pk)
    if not is_extract_anthology(book):
        raise ValidationError('To nie jest antologia Ekstraktów.')
    old = list(book.texts.select_for_update().exclude(import_source=SOURCE).order_by('pk'))
    snapshots = [_legacy_snapshot(text) for text in old]
    # Validation of all rows precedes deletion. Source Extract and Author records
    # and the volume credits/tasks are never deleted or recreated.
    for text in old:
        ExtractTextLink.objects.filter(text=text).delete()
        text.delete()
    whole = ensure_whole_text(book)
    if whole is None:
        raise ValidationError('Nie udało się utworzyć tekstu całej antologii.')
    return {'anthology_id': book.pk, 'anthology': book.title, 'text_id': whole.pk,
            'removed_generated_miniatures': len(old), 'snapshots': snapshots}
