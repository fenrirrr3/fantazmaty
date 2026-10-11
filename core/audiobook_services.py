"""Produkcja audiobooka – etapy jak w procesie tekstów.

Etap oczekuje (powstał, nie ma startu), trwa (ma datę rozpoczęcia albo znacznik
„data nieznana”) albo jest zakończony. Stan audiobooka („Czeka na korektę”,
„Trwa montaż”, „Po korekcie”…) wynika z etapów i jest tylko zapisywany jako
kopia do filtrowania przez refresh_status(). Wszystkie zmiany blokują tekst,
tak jak tokeny edycji.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from core.audiobook_models import StageType
from core.models import Audiobook, AudiobookStage
from core.permissions import is_coordinator, has_role, require_coordinator
from texts.models import Text
from texts.production import active_production_texts

Status = Audiobook.Status
STAGE_ORDER = tuple(StageType.values)
PROOFREADING_STATES = (Status.PROOFREADING_WAITING, Status.PROOFREADING)
# Po zakończeniu etapu: nagranie od razu trafia do korekty, montaż – do publikacji,
# po korekcie i po poprawkach o dalszym kroku decyduje koordynator.
AFTER_COMPLETION = {
    StageType.RECORDING: Status.PROOFREADING_WAITING,
    StageType.PROOFREADING: Status.AFTER_PROOFREADING,
    StageType.CORRECTIONS: Status.AFTER_CORRECTIONS,
    StageType.EDITING: Status.AWAITING_PUBLICATION,
}
NEXT_CHOICES = {
    Status.PENDING: STAGE_ORDER,  # zwykle Nagranie; pozostałe dla nagrań przygotowanych poza systemem
    Status.AFTER_PROOFREADING: (StageType.CORRECTIONS, StageType.EDITING),
    Status.AFTER_CORRECTIONS: (StageType.PROOFREADING, StageType.EDITING),
    Status.PROOFREADING_WAITING: (StageType.PROOFREADING,),  # stare dane: nagranie zakończone bez etapu korekty
    Status.AWAITING_PUBLICATION: (StageType.CORRECTIONS, StageType.EDITING),
}


def stage_label(stage_type):
    return dict(StageType.choices).get(stage_type, stage_type)


def status_label(status):
    return dict(Status.choices).get(status, status)


def ordered_stages(text):
    return list(AudiobookStage.objects.filter(text=text).order_by('position', 'pk'))


def compute_status(audio, stages, *, keep_published=True):
    if audio.paused_at:
        return Status.PAUSED, None
    open_stages = [stage for stage in stages if not stage.is_completed]
    if open_stages:
        stage = open_stages[-1]
        return (stage.stage_type if stage.is_started else f'{stage.stage_type}_waiting'), stage
    if audio.premiere_date or audio.youtube_url or audio.hearthis_url or audio.additional_links:
        return Status.PUBLISHED, None
    if keep_published and audio.status == Status.PUBLISHED:
        # Starsze publikacje bez daty i linków pozostają opublikowane (danych nie da się odtworzyć).
        return Status.PUBLISHED, None
    done = [stage for stage in stages if stage.is_completed]
    if not done:
        return Status.PENDING, None
    return AFTER_COMPLETION[done[-1].stage_type], None


def refresh_status(audio, stages=None, *, keep_published=True):
    """Jedyne miejsce ustawiające status i bieżący etap audiobooka."""
    stages = ordered_stages(audio.text_id) if stages is None else stages
    status, current = compute_status(audio, stages, keep_published=keep_published)
    if current is None and not audio.paused_at:
        current = next((stage for stage in reversed(stages) if not stage.is_completed), None)
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


def allowed_next_stages(audio):
    if audio is None or audio.paused_at or audio.active_stage_id:
        return []
    return list(NEXT_CHOICES.get(audio.status or Status.PENDING, ()))


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
    return bool(audio and not audio.paused_at and audio.status == Status.PROOFREADING_WAITING
                and audio.proofreader_id in (None, user.pk))


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


def _queue(text, stages, stage_type, queued_at):
    position = 1 + max((stage.position for stage in stages), default=0)
    stage = AudiobookStage.objects.create(text=text, stage_type=stage_type, position=position, queued_at=queued_at)
    stages.append(stage)
    return stage


@transaction.atomic
def queue_stage(*, text_id, user, stage_type):
    """Koordynator kieruje audiobook do etapu (decyzja po korekcie i po poprawkach)."""
    require_coordinator(user)
    text, audio = _lock(text_id, create=True)
    refresh_status(audio)
    if audio.paused_at:
        raise ValidationError('Prace są wstrzymane. Najpierw je wznów.')
    allowed = allowed_next_stages(audio)
    if stage_type not in allowed:
        names = ', '.join(stage_label(kind) for kind in allowed) or 'brak'
        raise ValidationError(f'Ten etap nie może teraz nastąpić. Dozwolone: {names}.')
    previous = audio.status_label
    stages = ordered_stages(text)
    _queue(text, stages, stage_type, timezone.localdate())
    if stage_type == StageType.PROOFREADING and audio.proofreader_id:
        # Kolejna korekta jest wolna do przejęcia (poprzedni korektor zachowuje swój zakończony etap).
        audio.proofreader = None
        audio.save(update_fields=['proofreader'])
    refresh_status(audio, stages)
    record_event(text, user, previous=previous, current=audio.status_label,
                 details=f'Skierowano do etapu: {stage_label(stage_type)}.')
    return audio


@transaction.atomic
def start_stage(*, text_id, user, started_at=None):
    """Rozpoczęcie oczekującego etapu. Bez daty (tylko koordynator) – data nieznana."""
    text, audio = _lock(text_id)
    if not audio or not audio.active_stage_id or audio.paused_at:
        raise ValidationError('Nie ma etapu oczekującego na rozpoczęcie.')
    stages = ordered_stages(text)
    stage = next(stage for stage in stages if stage.pk == audio.active_stage_id)
    coordinator = is_coordinator(user)
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
    stage = next((item for item in stages if item.pk == audio.active_stage_id), None)
    if stage is None:  # stare dane: nagranie zakończone bez utworzonego etapu korekty
        stage = _queue(text, stages, StageType.PROOFREADING, timezone.localdate())
    today = timezone.localdate()
    _check_date(today, label='Data rozpoczęcia', not_before=_last_known_end(stages))
    stage.started_at = today
    stage.start_unknown = False
    stage.performer = user
    stage.save(update_fields=['started_at', 'start_unknown', 'performer'])
    audio.proofreader = user
    audio.save(update_fields=['proofreader'])
    refresh_status(audio, stages)
    record_event(text, user, previous=status_label(Status.PROOFREADING_WAITING), current=audio.status_label,
                 details='Przejęto korektę audiobooka.', personal='Przejęcie: korekta audiobooka')
    return stage


@transaction.atomic
def finish_stage(*, text_id, stage_id, user, ended_at=None, unknown_date=False):
    """Zakończenie trwającego etapu. Korektor kończy własną korektę dzisiejszą datą."""
    text, audio = _lock(text_id)
    if not audio or not may_finish(user, audio):
        raise PermissionDenied('Możesz zakończyć tylko przypisaną sobie korektę audiobooka.')
    if audio.paused_at:
        raise ValidationError('Prace są wstrzymane. Najpierw je wznów.')
    if audio.active_stage_id != stage_id:
        raise ValidationError('Ten etap nie jest już aktualny. Odśwież podgląd.')
    stages = ordered_stages(text)
    stage = next(stage for stage in stages if stage.pk == stage_id)
    coordinator = is_coordinator(user)
    if not coordinator and stage.stage_type != StageType.PROOFREADING:
        raise PermissionDenied('Możesz zakończyć tylko etap korekty audiobooka.')
    if not stage.is_started:
        raise ValidationError('Etap jeszcze się nie rozpoczął.')
    if not coordinator or not unknown_date:
        ended_at = ended_at or timezone.localdate()
    else:
        ended_at = None
    _check_date(ended_at, label='Data zakończenia', not_before=stage.started_at)
    stage.ended_at = ended_at
    stage.is_completed = True
    if stage.stage_type == StageType.PROOFREADING and audio.proofreader_id:
        stage.performer_id = audio.proofreader_id
    stage.save(update_fields=['ended_at', 'is_completed', 'performer'])
    if stage.stage_type == StageType.RECORDING:
        _queue(text, stages, StageType.PROOFREADING, ended_at or timezone.localdate())
    previous = stage_label(stage.stage_type)
    refresh_status(audio, stages)
    own = stage.stage_type == StageType.PROOFREADING and stage.performer_id == user.pk
    record_event(text, user, previous=previous, current=audio.status_label,
                 details=f'Zakończono etap: {stage_label(stage.stage_type)}. Teraz: {audio.status_label}.',
                 personal='Zakończenie: korekta audiobooka' if own else '')
    return stage


@transaction.atomic
def set_paused(*, text_id, user, paused):
    require_coordinator(user)
    text, audio = _lock(text_id, create=True)
    if bool(audio.paused_at) == paused:
        raise ValidationError('Stan wstrzymania już jest taki. Odśwież podgląd.')
    previous = audio.status_label
    audio.paused_at = timezone.localdate() if paused else None
    audio.save(update_fields=['paused_at'])
    refresh_status(audio)
    record_event(text, user, previous=previous, current=audio.status_label,
                 details='Wstrzymano prace nad audiobookiem.' if paused else 'Wznowiono prace nad audiobookiem.')
    return audio


def status_filter(values, *, field='audiobook__status', today=None):
    """Warunek filtra statusów z uwzględnieniem premiery w przyszłości (to jeszcze nie publikacja)."""
    today = today or timezone.localdate()
    premiere = field.replace('status', 'premiere_date')
    published_now = Q(**{field: Status.PUBLISHED}) & (Q(**{f'{premiere}__isnull': True}) | Q(**{f'{premiere}__lte': today}))
    planned = Q(**{field: Status.PUBLISHED, f'{premiere}__gt': today})
    condition = Q(pk__in=[])
    plain = [value for value in values if value not in (Status.PUBLISHED, Status.AWAITING_PUBLICATION)]
    if plain:
        condition |= Q(**{f'{field}__in': plain})
    if Status.PUBLISHED in values:
        condition |= published_now
    if Status.AWAITING_PUBLICATION in values:
        condition |= Q(**{field: Status.AWAITING_PUBLICATION}) | planned
    return condition


def stage_periods(stages):
    """Okresy etapów do tabeli (oczekujące nie mają okresu)."""
    periods = {kind: [] for kind in STAGE_ORDER}
    for stage in stages:
        if stage.is_started:
            periods[stage.stage_type].append({'started_at': stage.started_at, 'ended_at': stage.ended_at,
                                              'is_completed': stage.is_completed})
    return periods


def next_position(text):
    return 1 + (AudiobookStage.objects.filter(text=text).aggregate(m=Max('position'))['m'] or 0)
