"""All stage mutations lock the Text aggregate, also used by edit tokens."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import Audiobook, AudiobookStage
from core.permissions import is_coordinator, has_role, require_coordinator
from texts.models import Text
from texts.production import active_production_texts


DATE_FIELDS = {
    'recording': 'recording_started_at', 'proofreading': 'proofreading_started_at',
    'corrections': 'corrections_started_at', 'editing': 'editing_started_at',
    'awaiting_publication': 'awaiting_publication_started_at',
}
# "Do nagrania" is the implicit state of every eligible text; it is never added as a stage.
STAGE_ORDER = ('recording', 'proofreading', 'corrections', 'editing', 'awaiting_publication', 'published')
FINAL_STAGE = 'published'


def stage_label(stage_type):
    return dict(Audiobook.Status.choices).get(stage_type, stage_type)


def allowed_next_stages(text, audio=None):
    """Stages follow production order; proofreading may be repeated after corrections.

    The audiobook status always equals the type of the most recently started stage.
    """
    if audio is None:
        audio = Audiobook.objects.filter(text=text).only('status').first()
    last = audio.status if audio and audio.status in STAGE_ORDER else None
    if last == FINAL_STAGE:
        return []
    if last is None:
        return list(STAGE_ORDER)
    index = STAGE_ORDER.index(last)
    allowed = list(STAGE_ORDER[index + 1:])
    if last in ('proofreading', 'corrections') and 'proofreading' not in allowed:
        allowed.insert(0, 'proofreading')
    return allowed


def record_event(text, user, *, previous, current, details, personal=''):
    """Audit and Discord notification for audiobook production (after commit)."""
    from core.models import WorkflowEvent
    from core.workflow_events import audiobook_channel, notify_after_commit
    person = getattr(user, 'person_profile', None)
    actor = str(person) if person else (user.get_full_name() or user.get_username())
    authors = ', '.join(a.display_name for a in text.authors.all())
    event = WorkflowEvent.objects.create(
        kind='audiobook', personal_work=bool(personal), personal_work_description=personal[:255],
        text=text, title=text.title, authors=authors, actor=user, actor_name=actor,
        previous_status=f'Audiobook: {previous}'[:100], next_status=f'Audiobook: {current}'[:100],
        details=details, channel=audiobook_channel())
    transaction.on_commit(lambda pk=event.pk: notify_after_commit(pk))
    return event


def status_text(audio):
    if not audio or audio.status == Audiobook.Status.PENDING:
        return stage_label(Audiobook.Status.PENDING)
    label = audio.get_status_display()
    if audio.status != FINAL_STAGE and not audio.active_stage_id:
        label += ' (zakończona)' if audio.status == 'proofreading' else ' (zakończony)'
    return label


def require_available(text):
    if not active_production_texts(Text.objects.filter(pk=text.pk, for_recording=True,
            audiobook_blacklisted=False)).exists():
        raise PermissionDenied('Tekst jest wyłączony z produkcji audiobooka. Zachowano dotychczasowe dane.')


def may_finish(user, audio):
    return bool(is_coordinator(user) or (audio and audio.status == Audiobook.Status.PROOFREADING
        and audio.proofreader_id == user.pk and has_role(user, 'Korektor audiobooków')))


def can_claim_proofreading(audio, history, user):
    if not audio or audio.status != Audiobook.Status.PROOFREADING or audio.proofreader_id not in (None, user.pk):
        return False
    if audio.active_stage_id:
        active = next((stage for stage in history if stage.pk == audio.active_stage_id), None)
        return bool(active and not active.is_completed and (audio.proofreader_id is None or active.started_at is None))
    return not any(stage.is_completed for stage in history)


@transaction.atomic
def claim_proofreading(*, text_id, user):
    from core.audiobook_forms import eligible_proofreaders
    if not eligible_proofreaders().filter(pk=user.pk).exists():
        raise PermissionDenied('Przejęcie wymaga aktywnej roli Korektor audiobooków.')
    text = Text.objects.select_for_update().get(pk=text_id)
    require_available(text)
    audio = Audiobook.objects.select_for_update().filter(text=text).first()
    history = list(AudiobookStage.objects.select_for_update().filter(text=text, stage_type='proofreading'))
    if not can_claim_proofreading(audio, history, user):
        raise ValidationError('Ta korekta nie jest już dostępna do przejęcia. Odśwież listę.')
    today = timezone.localdate()
    active = next((stage for stage in history if stage.pk == audio.active_stage_id), None)
    if active:
        if active.started_at and active.started_at > today:
            raise ValidationError('Korekta ma datę rozpoczęcia w przyszłości.')
        active.started_at = active.started_at or today
        active.performer = user
        active.save(update_fields=['started_at', 'performer'])
    else:
        started_at = audio.proofreading_started_at or today
        if started_at > today or text.audiobook_stages.filter(ended_at__gt=started_at).exists():
            raise ValidationError('Sprawdź datę rozpoczęcia korekty i zakończenia poprzedniego etapu.')
        active = AudiobookStage.objects.create(text=text, stage_type='proofreading', started_at=started_at, performer=user)
    audio.proofreader = user
    audio.active_stage = active
    audio.proofreading_started_at = active.started_at
    audio.save(update_fields=['proofreader', 'active_stage', 'proofreading_started_at'])
    record_event(text, user, previous=stage_label('proofreading'), current=stage_label('proofreading'),
                 details='Przejęto korektę audiobooka.', personal='Przejęcie: korekta audiobooka')
    return active


@transaction.atomic
def start_stage(*, text_id, user, stage_type, started_at):
    require_coordinator(user)
    text = Text.objects.select_for_update().get(pk=text_id)
    require_available(text)
    if stage_type not in STAGE_ORDER:
        raise ValidationError('Wybierz prawidłowy etap. „Do nagrania” jest stanem początkowym, nie etapem.')
    if not started_at or started_at > timezone.localdate():
        raise ValidationError('Podaj datę rozpoczęcia nie późniejszą niż dzisiaj.')
    audio, _ = Audiobook.objects.select_for_update().get_or_create(text=text)
    if audio.active_stage_id:
        raise ValidationError('Najpierw zakończ trwający etap.')
    if stage_type not in allowed_next_stages(text, audio):
        allowed = ', '.join(stage_label(kind) for kind in allowed_next_stages(text, audio)) or 'brak – audiobook jest opublikowany'
        raise ValidationError(f'Ten etap nie może teraz nastąpić. Dozwolone: {allowed}.')
    previous = text.audiobook_stages.filter(ended_at__isnull=False).order_by('-ended_at').first()
    if previous and started_at < previous.ended_at:
        raise ValidationError('Nowy etap nie może rozpocząć się przed zakończeniem poprzedniego.')
    previous_label = status_text(audio)
    final = stage_type == FINAL_STAGE
    # Publication is the last state: it is recorded as a completed stage and never "finished".
    stage = AudiobookStage.objects.create(text=text, stage_type=stage_type, started_at=started_at,
        ended_at=started_at if final else None, is_completed=final,
        performer=audio.proofreader if stage_type == Audiobook.Status.PROOFREADING else None)
    audio.active_stage = None if final else stage
    audio.status = stage_type
    fields = ['active_stage', 'status']
    if stage_type in DATE_FIELDS:
        field = DATE_FIELDS[stage_type]
        setattr(audio, field, started_at)
        fields.append(field)
    if final and not audio.premiere_date:
        audio.premiere_date = started_at
        fields.append('premiere_date')
    audio.save(update_fields=fields)
    record_event(text, user, previous=previous_label, current=stage_label(stage_type),
                 details=('Opublikowano audiobook' if final else f'Rozpoczęto etap: {stage_label(stage_type)}')
                 + f' ({started_at:%d.%m.%Y}).')
    return stage


@transaction.atomic
def finish_stage(*, text_id, stage_id, user):
    text = Text.objects.select_for_update().get(pk=text_id)
    require_available(text)
    audio = Audiobook.objects.filter(text=text).first()
    if not audio or not may_finish(user, audio):
        raise PermissionDenied('Możesz zakończyć tylko przypisaną sobie korektę audiobooka.')
    if audio.active_stage_id != stage_id:
        raise ValidationError('Ten etap nie jest już aktualny. Odśwież podgląd.')
    stage = AudiobookStage.objects.get(pk=stage_id, text=text)
    if not is_coordinator(user) and stage.stage_type != Audiobook.Status.PROOFREADING:
        raise PermissionDenied('Możesz zakończyć tylko etap korekty audiobooka.')
    if stage.is_completed or stage.stage_type == FINAL_STAGE:
        raise ValidationError('Etap został już zakończony.')
    today = timezone.localdate()
    if stage.started_at and stage.started_at > today:
        raise ValidationError('Etap ma datę rozpoczęcia w przyszłości.')
    stage.ended_at = today
    stage.is_completed = True
    if stage.stage_type == Audiobook.Status.PROOFREADING:
        stage.performer = audio.proofreader
    stage.save(update_fields=['ended_at', 'is_completed', 'performer'])
    audio.active_stage = None
    audio.save(update_fields=['active_stage'])
    own = stage.stage_type == Audiobook.Status.PROOFREADING and stage.performer_id == user.pk
    record_event(text, user, previous=stage_label(stage.stage_type), current=status_text(audio),
                 details=f'Zakończono etap: {stage_label(stage.stage_type)}. Czeka na kolejny etap.',
                 personal='Zakończenie: korekta audiobooka' if own else '')
    return stage
