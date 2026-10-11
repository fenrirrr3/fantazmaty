"""Read current proofreading alongside immutable performer history."""
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.urls import reverse

from core.models import Audiobook, AudiobookStage
from texts.models import Text
from texts.production import active_production_texts


OPEN_PROOFREADING = (Audiobook.Status.PROOFREADING_WAITING, Audiobook.Status.PROOFREADING)


def corrections():
    return AudiobookStage.objects.filter(stage_type='proofreading').select_related(
        'performer__person_profile').order_by('-position', '-pk')


def proofreading_scope(texts, user, coordinator, *, hide_completed=False):
    """Zakładka Korekta audiobooków: oczekujące, trwające i (opcjonalnie) zakończone korekty."""
    history = corrections().filter(text_id=OuterRef('pk'))
    eligible = active_production_texts(Text.objects.filter(for_recording=True, audiobook_blacklisted=False))
    texts = texts.filter(audiobook__isnull=False).annotate(
        has_audio_correction=Exists(history),
        own_audio_correction=Exists(history.filter(performer=user)),
        can_produce_audio=Exists(eligible.filter(pk=OuterRef('pk'))),
    )
    open_work = Q(audiobook__status__in=OPEN_PROOFREADING, audiobook__paused_at__isnull=True)
    if coordinator:
        texts = texts.filter(Q(has_audio_correction=True) | open_work)
        current = open_work
    else:
        from core.audiobook_forms import eligible_proofreaders
        free_work = Q(pk__in=[])
        if eligible_proofreaders().filter(pk=user.pk).exists():
            free_work = Q(can_produce_audio=True, audiobook__status=Audiobook.Status.PROOFREADING_WAITING,
                          audiobook__paused_at__isnull=True, audiobook__proofreader__isnull=True)
        mine = open_work & Q(audiobook__proofreader=user)
        texts = texts.filter(free_work | mine | Q(own_audio_correction=True))
        current = free_work | mine
    if hide_completed:
        texts = texts.filter(current)
    return texts.prefetch_related(Prefetch('audiobook_stages', queryset=corrections(), to_attr='audio_corrections'))


def can_assign_proofreader(audio, history):
    # Tylko bieżąca korekta (albo stare dane: korekta czeka, a etap jeszcze nie powstał).
    return bool(audio and audio.status in OPEN_PROOFREADING
                and (audio.active_stage_id or audio.status == Audiobook.Status.PROOFREADING_WAITING))


def performer_label(user):
    if not user:
        return 'Nie zapisano wykonawcy'
    person = getattr(user, 'person_profile', None)
    return str(person) if person else (user.get_full_name() or user.username)


def correction_rows(audio, history, *, viewer=None):
    rows = []
    for stage in history:
        current = stage.pk == audio.active_stage_id and not stage.is_completed
        # Zmiana przypisania dotyczy bieżącej korekty, nigdy zakończonej.
        performer = audio.proofreader if current else stage.performer
        waiting = current and not stage.is_started
        if viewer is not None and performer and performer.pk != viewer.pk:
            continue
        if viewer is not None and not performer and not waiting:
            continue
        if stage.is_completed:
            state = 'Zakończona'
        elif waiting:
            state = 'Przypisana – czeka na rozpoczęcie' if performer else 'Czeka na przejęcie'
        else:
            state = 'W toku' if current else 'Nieaktywna'
        rows.append({'pk': stage.pk, 'performer_id': performer.pk if performer else None,
            'person_id': getattr(getattr(performer, 'person_profile', None), 'pk', None),
            'performer': performer_label(performer), 'started_at': stage.started_at,
            'start_unknown': stage.start_unknown, 'ended_at': stage.ended_at, 'is_completed': stage.is_completed,
            'is_active': current and not waiting, 'is_waiting': waiting, 'state': state})
    return rows


def profile_audio_assignments(person):
    if not person.user_id:
        return []
    audio_rows = Audiobook.objects.filter(Q(proofreader_id=person.user_id)
        | Q(text__audiobook_stages__stage_type='proofreading', text__audiobook_stages__performer_id=person.user_id,
            text__audiobook_stages__is_completed=True)).distinct().select_related(
                'proofreader__person_profile', 'text__anthology', 'text__translation').prefetch_related(
                    'text__authors', 'text__translation__foreign_authors',
                    Prefetch('text__audiobook_stages', queryset=corrections(), to_attr='audio_corrections'))
    result = []
    for audio in audio_rows:
        history = audio.text.audio_corrections
        if not history:
            continue
        text = audio.text
        translated = bool(text.anthology_id and text.anthology.is_translated)
        translation = getattr(text, 'translation', None) if translated else None
        authors = translation.foreign_authors.all() if translation else ([] if translated else text.authors.all())
        for stage in correction_rows(audio, history, viewer=person.user):
            result.append({'pk': stage['pk'], 'kind': 'Audiobook', 'kind_key': 'audio',
                'detail_url': reverse('core:audiobook_detail', args=[text.pk]),
                'role': 'audio_proofreader', 'get_role_display': 'Korektor audiobooków', 'assigned_at': None,
                'has_active_work': stage['is_active'], 'has_completed_work': stage['is_completed'],
                'has_reserved_work': stage['is_waiting'] and stage['performer_id'] is not None, 'state_label': stage['state'],
                'latest_stage': {**stage, 'imported_completed': stage['is_completed']},
                'text': {'pk': text.pk, 'title': text.title, 'is_translation': translated,
                    'anthology': {'pk': text.anthology_id, 'title': text.anthology.title} if text.anthology_id else None,
                    'authors': {'all': [{'pk': a.pk, 'display_name': a.display_name} for a in authors]}}})
    return result
