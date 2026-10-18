"""Audiobooki – wpisy prowadzone przez koordynatora (jak ilustracje), z historią etapów jak w obiegu tekstów.

Status (Nieprzypisane → Nagrywanie → Korekta → Nanoszenie poprawek → Montaż → Opublikowane) ustawia
koordynator; zmiana statusu zamyka bieżący etap i otwiera etap nowego statusu z datą (pusta = nieznana).
Daty etapów i wykonawcę korekt koordynator może poprawić w tabeli etapów, a etap inny niż bieżący – usunąć. Jedynym obiegiem pracy jest korekta: status „Korekta”
udostępnia ją korektorom audiobooków, korektor przejmuje ją i kończy, a status przechodzi na
„Nanoszenie poprawek”. Lektor i montażysta to wpisy spisu – nie potrzebują kont.
Zmiany blokują tekst, tak jak tokeny edycji.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.audiobook_models import StageType
from core.models import Audiobook, AudiobookStage
from core.permissions import is_coordinator, has_role
from people.roles import AUDIOBOOK_PROOFREADER
from texts.models import Text
from texts.production import active_production_texts

Status = Audiobook.Status
STAGE_ORDER = tuple(StageType.values)
NEXT_STAGE = dict(zip(STAGE_ORDER, STAGE_ORDER[1:]))


def stage_label(stage_type):
    return dict(StageType.choices).get(stage_type, stage_type)


def ordered_stages(text):
    return list(AudiobookStage.objects.filter(text=text).order_by('position', 'pk'))


def _activate(audio, status, stage):
    """Jedyne miejsce ustawiające status i bieżący etap audiobooka."""
    audio.status = status
    audio.active_stage = stage
    audio.save(update_fields=['status', 'active_stage'])
    return audio


def record_event(text, user, *, previous, current, details, personal=''):
    """Wpis produkcji audiobooka w dzienniku zmian (WorkflowEvent) – bez powiadomienia Discord."""
    from core.models import WorkflowEvent
    person = getattr(user, 'person_profile', None)
    actor = str(person) if person else (user.get_full_name() or user.get_username())
    authors = ', '.join(a.display_name for a in text.authors.all())
    return WorkflowEvent.objects.create(
        kind='audiobook', personal_work=bool(personal), personal_work_description=personal[:255],
        text=text, title=text.title, authors=authors, actor=user, actor_name=actor,
        previous_status=f'Audiobook: {previous}'[:100], next_status=f'Audiobook: {current}'[:100],
        details=details)


def require_available(text):
    if not active_production_texts(Text.objects.filter(pk=text.pk, for_recording=True,
            audiobook_blacklisted=False)).exists():
        raise PermissionDenied('Tekst jest wyłączony z produkcji audiobooka. Zachowano dotychczasowe dane.')


def may_finish(user, audio):
    return bool(is_coordinator(user) or (audio and audio.status == Status.PROOFREADING
        and audio.proofreader_id == user.pk and has_role(user, AUDIOBOOK_PROOFREADER)))


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


def _require_coordinator(user):
    if not is_coordinator(user):
        raise PermissionDenied('Wpisy audiobooków zmienia koordynator.')


def _close(audio, stage, ended_at):
    """Zamyka bieżący etap przy zmianie statusu. Nieprzejęta korekta znika – nikt jej nie wykonał."""
    if stage.stage_type == StageType.PROOFREADING and not stage.is_started:
        _activate(audio, audio.status, None)
        stage.delete()
        return
    _check_date(ended_at, label='Data zakończenia', not_before=stage.started_at)
    stage.start_unknown = stage.start_unknown or not stage.started_at
    stage.ended_at = ended_at
    stage.is_completed = True
    if stage.stage_type == StageType.PROOFREADING and audio.proofreader_id and not stage.performer_id:
        stage.performer_id = audio.proofreader_id
    stage.save(update_fields=['start_unknown', 'ended_at', 'is_completed', 'performer'])


@transaction.atomic
def set_status(*, text_id, user, status, date=None):
    """Koordynator ustawia status: bieżący etap się kończy, etap nowego statusu zaczyna się w podanym dniu
    (pusta data – nieznana). Korekta czeka na przejęcie przez korektora (albo przypisanie)."""
    _require_coordinator(user)
    if status not in Status.values:
        raise ValidationError('Wybierz prawidłowy status.')
    text, audio = _lock(text_id, create=True)
    if audio.status == status:
        raise ValidationError('Audiobook ma już ten status.')
    _check_date(date, label='Data')
    stages = ordered_stages(text)
    previous = audio.status_label
    current = next((stage for stage in stages if stage.pk == audio.active_stage_id), None)
    if current is not None:
        _close(audio, current, date)
    stage = None
    if status in STAGE_ORDER:
        stage = _add_stage(text, stages, status, date or timezone.localdate())
        if status == StageType.PROOFREADING:
            audio.proofreader = None  # nowa korekta – do przejęcia
            audio.save(update_fields=['proofreader'])
        else:
            stage.started_at = date
            stage.start_unknown = date is None
            stage.save(update_fields=['started_at', 'start_unknown'])
    _activate(audio, status, stage)
    record_event(text, user, previous=previous, current=audio.status_label,
                 details=f'Zmieniono status: {audio.status_label}' + (f' ({date:%d.%m.%Y}).' if date else '.'))
    return audio


def _set_performer(audio, stage, performer_id):
    """Zmiana wykonawcy korekty; nowa osoba musi mieć rolę Korektor audiobooków (historyczna zostaje bez sprawdzania).
    Wykonawca bieżącej korekty jest zarazem przypisanym korektorem audiobooka."""
    from core.audiobook_forms import eligible_proofreaders
    if performer_id and not eligible_proofreaders().filter(pk=performer_id).exists():
        raise ValidationError(f'{stage_label(stage.stage_type)}: wybierz aktywne konto z rolą Korektor audiobooków.')
    stage.performer_id = performer_id
    if audio.active_stage_id == stage.pk and audio.proofreader_id != performer_id:
        audio.proofreader_id = performer_id
        audio.save(update_fields=['proofreader'])


@transaction.atomic
def update_stage_dates(*, text_id, user, dates, performers=None):
    """Poprawa etapów przez koordynatora: dates = {id etapu: (rozpoczęcie, zakończenie)},
    performers = {id etapu korekty: id wykonawcy albo None}. Zwraca etykiety zmienionych etapów."""
    _require_coordinator(user)
    text, audio = _lock(text_id)
    if audio is None:
        raise ValidationError('Audiobook nie ma jeszcze etapów.')
    performers = performers or {}
    changed, details = [], []
    for stage in ordered_stages(text):
        label = stage_label(stage.stage_type)
        fields = set()
        if stage.stage_type == StageType.PROOFREADING and stage.pk in performers and performers[stage.pk] != stage.performer_id:
            _set_performer(audio, stage, performers[stage.pk])
            fields.add('performer')
            details.append(f'{label}: wykonawca')
        if stage.pk in dates:
            started_at, ended_at = dates[stage.pk]
            _check_date(started_at, label=f'{label}: data rozpoczęcia')
            _check_date(ended_at, label=f'{label}: data zakończenia', not_before=started_at)
            if ended_at and not stage.is_completed:
                raise ValidationError(f'{label}: etap trwa – zakończ go zmianą statusu.')
            if (stage.started_at, stage.ended_at) != (started_at, ended_at):
                if started_at:
                    stage.start_unknown = False
                    if stage.stage_type == StageType.PROOFREADING and not stage.performer_id and stage.pk not in performers:
                        stage.performer_id = audio.proofreader_id
                elif stage.started_at:
                    stage.start_unknown = True  # usunięcie daty nie cofa rozpoczęcia – data staje się nieznana
                stage.started_at, stage.ended_at = started_at, ended_at
                fields |= {'started_at', 'ended_at', 'start_unknown', 'performer'}
                details.append(f'{label}: daty')
        if fields:
            stage.save(update_fields=fields)
            changed.append(label)
    if changed:
        record_event(text, user, previous=audio.status_label, current=audio.status_label,
                     details='Poprawiono etapy – ' + ', '.join(details) + '.')
    return changed


@transaction.atomic
def delete_stage(*, text_id, user, stage_id):
    """Usunięcie etapu z historii przez koordynatora. Bieżącego etapu nie usuwa się – zamyka go zmiana statusu.
    Pozycje pozostałych etapów zostają bez zmian."""
    _require_coordinator(user)
    text, audio = _lock(text_id)
    stage = AudiobookStage.objects.filter(text=text, pk=stage_id).first()
    if audio is None or stage is None:
        raise ValidationError('Tego etapu już nie ma. Odśwież podgląd.')
    if audio.active_stage_id == stage.pk:
        raise ValidationError('Bieżący etap zamyka zmiana statusu – nie można go usunąć.')
    label = stage_label(stage.stage_type)
    period = ' – '.join(f'{day:%d.%m.%Y}' for day in (stage.started_at, stage.ended_at) if day)
    stage.delete()
    record_event(text, user, previous=audio.status_label, current=audio.status_label,
                 details=f'Usunięto etap: {label}' + (f' ({period}).' if period else '.'))
    return stage


@transaction.atomic
def start_stage(*, text_id, user, started_at=None):
    """Rozpoczęcie przypisanej korekty (korektor – dziś; koordynator – podana data albo nieznana)."""
    text, audio = _lock(text_id)
    stages = ordered_stages(text)
    stage = _current(audio, stages) if audio else None
    if stage is None or stage.stage_type != StageType.PROOFREADING:
        raise ValidationError('Rozpocząć można tylko korektę audiobooka.')
    coordinator = is_coordinator(user)
    own_proofreading = audio.proofreader_id == user.pk and has_role(user, AUDIOBOOK_PROOFREADER)
    if not coordinator and not own_proofreading:
        raise PermissionDenied('Korektę rozpoczyna przypisany korektor albo koordynator.')
    if stage.is_started:
        raise ValidationError('Ten etap już trwa. Odśwież podgląd.')
    if started_at is None and not coordinator:
        started_at = timezone.localdate()
    _check_date(started_at, label='Data rozpoczęcia', not_before=_last_known_end(stages))
    stage.started_at = started_at
    stage.start_unknown = started_at is None
    stage.performer_id = audio.proofreader_id
    stage.save(update_fields=['started_at', 'start_unknown', 'performer'])
    _activate(audio, audio.status, stage)
    record_event(text, user, previous=audio.status_label, current=audio.status_label,
                 details='Rozpoczęto korektę audiobooka' + (f' ({started_at:%d.%m.%Y}).' if started_at else ' (data nieznana).'))
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
    _activate(audio, audio.status, stage)
    record_event(text, user, previous='Korekta – do przejęcia', current=audio.status_label,
                 details='Przejęto korektę audiobooka.', personal='Przejęcie: korekta audiobooka')
    return stage


@transaction.atomic
def finish_stage(*, text_id, stage_id, user, ended_at=None, unknown_date=False):
    """Zakończenie trwającej korekty; status przechodzi na „Nanoszenie poprawek” (etap czeka na datę rozpoczęcia)."""
    text, audio = _lock(text_id)
    if not audio or not may_finish(user, audio):
        raise PermissionDenied('Możesz zakończyć tylko przypisaną sobie korektę audiobooka.')
    if audio.active_stage_id != stage_id:
        raise ValidationError('Ten etap nie jest już aktualny. Odśwież podgląd.')
    stages = ordered_stages(text)
    stage = next(stage for stage in stages if stage.pk == stage_id)
    if stage.stage_type != StageType.PROOFREADING:
        raise ValidationError('Pozostałe etapy kończy zmiana statusu.')
    if not stage.is_started:
        raise ValidationError('Etap jeszcze się nie rozpoczął.')
    coordinator = is_coordinator(user)
    ended_at = None if coordinator and unknown_date else (ended_at or timezone.localdate())
    _check_date(ended_at, label='Data zakończenia', not_before=stage.started_at)
    stage.ended_at = ended_at
    stage.is_completed = True
    if audio.proofreader_id:
        stage.performer_id = audio.proofreader_id
    stage.save(update_fields=['ended_at', 'is_completed', 'performer'])
    following = _add_stage(text, stages, StageType.CORRECTIONS, ended_at or timezone.localdate())
    _activate(audio, Status.CORRECTIONS, following)
    own = stage.performer_id == user.pk
    record_event(text, user, previous=stage_label(stage.stage_type), current=audio.status_label,
                 details=f'Zakończono korektę audiobooka. Teraz: {audio.status_label}.',
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

