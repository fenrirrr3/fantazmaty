"""Produkcja audiobooka – etapy w stałej kolejności.

Do wzięcia → Nagrywanie → Korekta → Nanoszenie poprawek → Montaż → Opublikowane.
Rozpoczęcie nagrywania to wzięcie tekstu. Zakończenie etapu udostępnia kolejny
(czeka na rozpoczęcie; korektę rozpoczyna korektor, przejmując ją). Zakończenie
montażu oznacza publikację – data premiery i linki są danymi tego etapu.
Wszystkie daty są opcjonalne. Status audiobooka jest tylko kopią do filtrowania
ustawianą przez refresh_status(). Zmiany blokują tekst, tak jak tokeny edycji.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.audiobook_models import StageType
from core.models import Audiobook, AudiobookStage
from core.permissions import is_coordinator, has_role
from texts.models import Text
from texts.production import active_production_texts

Status = Audiobook.Status
STAGE_ORDER = tuple(StageType.values)
NEXT_STAGE = dict(zip(STAGE_ORDER, STAGE_ORDER[1:]))


def stage_label(stage_type):
    return dict(StageType.choices).get(stage_type, stage_type)


def status_label(status):
    return dict(Status.choices).get(status, status)


def ordered_stages(text):
    return list(AudiobookStage.objects.filter(text=text).order_by('position', 'pk'))


def compute_status(audio, stages):
    """Status i bieżący etap: ostatni niezakończony etap; po montażu (albo z publikacją) – Opublikowane."""
    stages = [stage for stage in stages if stage.stage_type in STAGE_ORDER]
    open_stages = [stage for stage in stages if not stage.is_completed]
    if open_stages:
        return open_stages[-1].stage_type, open_stages[-1]
    done = [stage for stage in stages if stage.is_completed]
    if (done and done[-1].stage_type == StageType.EDITING) or audio.premiere_date or audio.youtube_url \
            or audio.hearthis_url or audio.additional_links or audio.status == Status.PUBLISHED:
        return Status.PUBLISHED, None
    if done:
        # Stare dane: etap zakończony bez utworzonego następnego – status to następny etap.
        return NEXT_STAGE[done[-1].stage_type], None
    return Status.PENDING, None


def refresh_status(audio, stages=None):
    """Jedyne miejsce ustawiające status i bieżący etap audiobooka."""
    stages = ordered_stages(audio.text_id) if stages is None else stages
    status, current = compute_status(audio, stages)
    fields = []
    if audio.status != status:
        audio.status = status
        fields.append('status')
    if audio.active_stage_id != (current.pk if current else None):
        audio.active_stage = current
        fields.append('active_stage')
    if fields:
        audio.save(update_fields=fields)
    return audio


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
    return audio.status_label if audio else status_label(Status.PENDING)


def require_available(text):
    if not active_production_texts(Text.objects.filter(pk=text.pk, for_recording=True,
            audiobook_blacklisted=False)).exists():
        raise PermissionDenied('Tekst jest wyłączony z produkcji audiobooka. Zachowano dotychczasowe dane.')


def may_finish(user, audio):
    return bool(is_coordinator(user) or (audio and audio.status == Status.PROOFREADING
        and audio.proofreader_id == user.pk and has_role(user, 'Korektor audiobooków')))


def can_claim_proofreading(audio, history, user):
    stage = audio.active_stage if audio and audio.active_stage_id else None
    waiting = stage is None or not stage.is_started  # stare dane: korekta bez utworzonego etapu
    return bool(audio and audio.status == Status.PROOFREADING and waiting and audio.proofreader_id in (None, user.pk))


def _lock(text_id, *, create=False):
    text = Text.objects.select_for_update().get(pk=text_id)
    require_available(text)
    if create:
        audio, _ = Audiobook.objects.select_for_update().get_or_create(text=text)
    else:
        audio = Audiobook.objects.select_for_update().filter(text=text).first()
    return text, audio


def _check_date(value, *, label, not_before=None):
    if value is None:
        return
    if value > timezone.localdate():
        raise ValidationError(f'{label} nie może być w przyszłości.')
    if not_before and value < not_before:
        raise ValidationError(f'{label} nie może poprzedzać {not_before:%d.%m.%Y}.')


def _last_known_end(stages):
    ends = [stage.ended_at for stage in stages if stage.ended_at]
    return max(ends) if ends else None


def _add_stage(text, stages, stage_type, queued_at):
    position = 1 + max((stage.position for stage in stages), default=0)
    stage = AudiobookStage.objects.create(text=text, stage_type=stage_type, position=position, queued_at=queued_at)
    stages.append(stage)
    return stage


def _current(audio, stages):
    """Bieżący etap; dla starych danych (status bez etapu) tworzy go."""
    stage = next((item for item in stages if item.pk == audio.active_stage_id), None)
    if stage is None and audio.status in STAGE_ORDER:
        stage = _add_stage(audio.text, stages, audio.status, None)
    return stage


@transaction.atomic
def start_stage(*, text_id, user, started_at=None):
    """Rozpoczęcie bieżącego etapu (z „Do wzięcia” – nagrywania). Bez daty (koordynator) – data nieznana."""
    text, audio = _lock(text_id, create=True)
    stages = ordered_stages(text)
    coordinator = is_coordinator(user)
    if audio.status == Status.PENDING and not audio.active_stage_id:
        if not coordinator:
            raise PermissionDenied('Nagrywanie rozpoczyna koordynator.')
        stage = _add_stage(text, stages, StageType.RECORDING, None)
    elif audio.status == Status.PUBLISHED:
        raise ValidationError('Audiobook jest opublikowany – nie ma etapu do rozpoczęcia.')
    else:
        stage = _current(audio, stages)
    own_proofreading = (stage.stage_type == StageType.PROOFREADING and audio.proofreader_id == user.pk
                        and has_role(user, 'Korektor audiobooków'))
    if not coordinator and not own_proofreading:
        raise PermissionDenied('Etap rozpoczyna koordynator albo przypisany korektor.')
    if stage.is_started:
        raise ValidationError('Ten etap już trwa. Odśwież podgląd.')
    if started_at is None and not coordinator:
        started_at = timezone.localdate()
    _check_date(started_at, label='Data rozpoczęcia', not_before=_last_known_end(stages))
    stage.started_at = started_at
    stage.start_unknown = started_at is None
    if stage.stage_type == StageType.PROOFREADING:
        stage.performer_id = audio.proofreader_id
    stage.save(update_fields=['started_at', 'start_unknown', 'performer'])
    previous = audio.status_label
    refresh_status(audio, stages)
    record_event(text, user, previous=previous, current=audio.status_label,
                 details=f'Rozpoczęto etap: {stage_label(stage.stage_type)}'
                 + (f' ({started_at:%d.%m.%Y}).' if started_at else ' (data nieznana).'))
    return stage


@transaction.atomic
def claim_proofreading(*, text_id, user):
    from core.audiobook_forms import eligible_proofreaders
    if not eligible_proofreaders().filter(pk=user.pk).exists():
        raise PermissionDenied('Przejęcie wymaga aktywnej roli Korektor audiobooków.')
    text, audio = _lock(text_id)
    if not can_claim_proofreading(audio, None, user):
        raise ValidationError('Ta korekta nie jest już dostępna do przejęcia. Odśwież listę.')
    stages = ordered_stages(text)
    stage = _current(audio, stages)
    today = timezone.localdate()
    _check_date(today, label='Data rozpoczęcia', not_before=_last_known_end(stages))
    stage.started_at = today
    stage.start_unknown = False
    stage.performer = user
    stage.save(update_fields=['started_at', 'start_unknown', 'performer'])
    audio.proofreader = user
    audio.save(update_fields=['proofreader'])
    refresh_status(audio, stages)
    record_event(text, user, previous='Korekta – do przejęcia', current=audio.status_label,
                 details='Przejęto korektę audiobooka.', personal='Przejęcie: korekta audiobooka')
    return stage


@transaction.atomic
def finish_stage(*, text_id, stage_id, user, ended_at=None, unknown_date=False):
    """Zakończenie trwającego etapu; następny etap czeka na rozpoczęcie, po montażu – Opublikowane."""
    text, audio = _lock(text_id)
    if not audio or not may_finish(user, audio):
        raise PermissionDenied('Możesz zakończyć tylko przypisaną sobie korektę audiobooka.')
    if audio.active_stage_id != stage_id:
        raise ValidationError('Ten etap nie jest już aktualny. Odśwież podgląd.')
    stages = ordered_stages(text)
    stage = next(stage for stage in stages if stage.pk == stage_id)
    coordinator = is_coordinator(user)
    if not coordinator and stage.stage_type != StageType.PROOFREADING:
        raise PermissionDenied('Możesz zakończyć tylko etap korekty audiobooka.')
    if not stage.is_started:
        raise ValidationError('Etap jeszcze się nie rozpoczął.')
    ended_at = None if coordinator and unknown_date else (ended_at or timezone.localdate())
    _check_date(ended_at, label='Data zakończenia', not_before=stage.started_at)
    stage.ended_at = ended_at
    stage.is_completed = True
    if stage.stage_type == StageType.PROOFREADING and audio.proofreader_id:
        stage.performer_id = audio.proofreader_id
    stage.save(update_fields=['ended_at', 'is_completed', 'performer'])
    following = NEXT_STAGE.get(stage.stage_type)
    if following:
        _add_stage(text, stages, following, ended_at or timezone.localdate())
    refresh_status(audio, stages)
    own = stage.stage_type == StageType.PROOFREADING and stage.performer_id == user.pk
    record_event(text, user, previous=stage_label(stage.stage_type), current=audio.status_label,
                 details=f'Zakończono etap: {stage_label(stage.stage_type)}. Teraz: {audio.status_label}.',
                 personal='Zakończenie: korekta audiobooka' if own else '')
    return stage


def stage_periods(stages):
    """Okresy etapów do tabeli (etap czekający na rozpoczęcie nie ma okresu)."""
    periods = {kind: [] for kind in STAGE_ORDER}
    for stage in stages:
        if stage.is_started and stage.stage_type in periods:
            periods[stage.stage_type].append({'started_at': stage.started_at, 'ended_at': stage.ended_at,
                                              'is_completed': stage.is_completed})
    return periods


def next_position(text):
    return 1 + (AudiobookStage.objects.filter(text=text).aggregate(m=Max('position'))['m'] or 0)
