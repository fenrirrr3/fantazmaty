"""Novel operations reuse the existing chapter workflow and its eligibility rules."""
import hashlib
import json
from contextlib import contextmanager

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.permissions import is_coordinator
from core.workflow_events import track_workflow
from texts.models import Anthology, NovelProfile, Text
from workflow.models import WorkflowStage, WorkflowRoleAssignment
from workflow.services import _assign_role, create_pending_stage


def signature(book):
    """Includes workflow, membership, order and metadata, never the approval itself."""
    profile = NovelProfile.objects.filter(anthology=book).values(
        'tags', 'genre', 'content_warnings', 'notes', 'file_url').first()
    data = {
        'book': Anthology.objects.filter(pk=book.pk).values().first(),
        'profile': profile,
        'authors': list(NovelProfile.objects.filter(anthology=book).values_list('authors', flat=True).order_by('authors')),
        'chapters': list(Text.objects.filter(anthology=book).order_by('pk').values()),
        'stages': list(WorkflowStage.objects.filter(text__anthology=book).order_by('pk').values()),
        'assignments': list(WorkflowRoleAssignment.objects.filter(text__anthology=book).order_by('pk').values()),
        'tasks': list(book.production_tasks.order_by('pk').values()),
    }
    # Switching the book to ready must not invalidate its own approval.
    data['book'].pop('status', None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def edit_token(book, user):
    return signing.dumps({'book': book.pk, 'user': user.pk, 'signature': signature(book)}, salt='novel-edit')


@contextmanager
def locked_book(book_id, user, token):
    if not is_coordinator(user):
        raise PermissionDenied
    with transaction.atomic():
        # Existing workflow locks Text before Anthology; keep the same order.
        list(Text.objects.select_for_update().filter(anthology_id=book_id).order_by('pk'))
        book = Anthology.objects.select_for_update().get(pk=book_id, is_novel=True)
        try:
            payload = signing.loads(token, salt='novel-edit', max_age=86400)
        except signing.BadSignature as exc:
            raise ValidationError('Formularz wygasł. Odśwież stronę i spróbuj ponownie.') from exc
        if payload != {'book': book.pk, 'user': user.pk, 'signature': signature(book)}:
            raise ValidationError('Powieść lub jej rozdziały zostały zmienione. Odśwież stronę; nic nie zapisano.')
        yield book


def require_open(book):
    if book.status == Anthology.Status.READY:
        raise ValidationError('Najpierw wznów pracę nad powieścią.')


def chapters_ready(book):
    chapters = list(Text.objects.filter(anthology=book))
    if not chapters:
        return False
    active = 0
    for chapter in chapters:
        stages = chapter.workflow_stages.filter(workflow_cycle=chapter.current_workflow_cycle, is_current=True)
        if stages.filter(stage_type=WorkflowStage.StageType.WITHDRAWN).exists():
            continue
        active += 1
        if not stages.filter(stage_type=WorkflowStage.StageType.READY, is_released=True).exists():
            return False
        if stages.exclude(stage_type__in=[WorkflowStage.StageType.READY, WorkflowStage.StageType.WITHDRAWN]).filter(is_completed=False, is_released=True).exists():
            return False
    return bool(active)


def validate_ready_novel(book):
    profile = NovelProfile.objects.filter(anthology=book).first()
    if not chapters_ready(book):
        raise ValidationError('Powieść musi mieć ukończone wszystkie niewycofane rozdziały.')
    if not profile or not profile.approved_signature or profile.approved_signature != signature(book):
        raise ValidationError('Zatwierdź końcową kontrolę spójności powieści po ostatniej zmianie.')


def approve(book, user):
    require_open(book)
    if not chapters_ready(book):
        raise ValidationError('Najpierw zakończ wszystkie niewycofane rozdziały.')
    profile = NovelProfile.objects.get(anthology=book)
    profile.approved_signature = signature(book)
    profile.approved_at = timezone.now()
    profile.approved_by = user
    profile.save(update_fields=['approved_signature', 'approved_at', 'approved_by'])


def sync_metadata(book, profile):
    authors = list(profile.authors.all())
    for chapter in Text.objects.filter(anthology=book).order_by('pk'):
        chapter.authors.set(authors)
        chapter.tags, chapter.genre = profile.tags, profile.genre
        chapter.save(update_fields=['tags', 'genre'])


def new_chapter(book, profile, **fields):
    require_open(book)
    chapter = Text(anthology=book, for_recording=False, tags=profile.tags, genre=profile.genre, **fields)
    chapter.full_clean()
    chapter.save()
    chapter.authors.set(profile.authors.all())
    create_pending_stage(chapter, WorkflowStage.StageType.READY_FOR_EDITING)
    return chapter


@track_workflow
def assign_chapter(text, user, role, assignee):
    from core.services.texts import _require_eligible_assignee
    from workflow.services import STAGE_ROLES
    _require_eligible_assignee(assignee, role, lock=True)
    stages = text.workflow_stages.filter(workflow_cycle=text.current_workflow_cycle, is_current=True)
    if stages.filter(stage_type__in=[WorkflowStage.StageType.READY, WorkflowStage.StageType.WITHDRAWN]).exists():
        raise ValidationError(f'Rozdział „{text.title}” jest zakończony lub wycofany.')
    role_stages = [stage for stage, assigned_role in STAGE_ROLES.items() if assigned_role == role]
    if stages.filter(stage_type__in=role_stages, is_completed=True).exists():
        raise ValidationError(f'Rozdział „{text.title}”: ta praca została już wykonana. Użyj wznowienia w podglądzie rozdziału.')
    _assign_role(text, role, assignee)


def assign_chapters(book, user, chapter_ids, assignments):
    require_open(book)
    ids = set(chapter_ids)
    chapters = list(Text.objects.filter(anthology=book, pk__in=ids).order_by('pk'))
    if not ids or len(chapters) != len(ids):
        raise ValidationError('Wybierz rozdziały należące do tej powieści.')
    if not assignments:
        raise ValidationError('Wybierz przynajmniej jedną rolę i wykonawcę.')
    if len({role for role, _ in assignments}) != len(assignments):
        raise ValidationError('W jednym przydziale wybierz każdą rolę tylko raz.')
    for chapter in chapters:
        for role, assignee in assignments:
            assign_chapter(chapter, user, role, assignee)
