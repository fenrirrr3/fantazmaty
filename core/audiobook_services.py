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
    return active


@transaction.atomic
def start_stage(*, text_id, user, stage_type, started_at):
    require_coordinator(user)
    text = Text.objects.select_for_update().get(pk=text_id)
    require_available(text)
    if stage_type not in Audiobook.Status.values:
        raise ValidationError('Wybierz prawidłowy etap.')
    if not started_at or started_at > timezone.localdate():
        raise ValidationError('Podaj datę rozpoczęcia nie późniejszą niż dzisiaj.')
    audio, _ = Audiobook.objects.get_or_create(text=text)
    if audio.active_stage_id:
        raise ValidationError('Najpierw zakończ trwający etap.')
    previous = text.audiobook_stages.filter(ended_at__isnull=False).order_by('-ended_at').first()
    if previous and started_at < previous.ended_at:
        raise ValidationError('Nowy etap nie może rozpocząć się przed zakończeniem poprzedniego.')
    stage = AudiobookStage.objects.create(text=text, stage_type=stage_type, started_at=started_at,
        performer=audio.proofreader if stage_type == Audiobook.Status.PROOFREADING else None)
    audio.active_stage = stage
    audio.status = stage_type
    fields = ['active_stage', 'status']
    if stage_type in DATE_FIELDS:
        field = DATE_FIELDS[stage_type]
        setattr(audio, field, started_at)
        fields.append(field)
    audio.save(update_fields=fields)
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
    if stage.is_completed:
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
    return stage
