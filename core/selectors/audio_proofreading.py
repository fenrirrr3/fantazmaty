"""Read current proofreading alongside immutable performer history."""
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.urls import reverse

from core.models import Audiobook, AudiobookStage
from texts.models import Text
from texts.production import active_production_texts


def corrections():
    return AudiobookStage.objects.filter(stage_type=Audiobook.Status.PROOFREADING).select_related(
        'performer__person_profile').order_by('-pk')


def proofreading_scope(texts, user, coordinator, *, hide_completed=False):
    history = corrections().filter(text_id=OuterRef('pk'))
    eligible = active_production_texts(Text.objects.filter(for_recording=True, audiobook_blacklisted=False))
    texts = texts.filter(audiobook__isnull=False).annotate(
        has_audio_correction=Exists(history),
        has_finished_audio_correction=Exists(history.filter(is_completed=True)),
        own_audio_correction=Exists(history.filter(performer=user, is_completed=True)),
        can_produce_audio=Exists(eligible.filter(pk=OuterRef('pk'))),
    ).filter(Q(has_audio_correction=True) | Q(audiobook__proofreading_started_at__isnull=False)
        | Q(pk__in=eligible.values('pk'), audiobook__status=Audiobook.Status.PROOFREADING))
    if not coordinator:
        from core.audiobook_forms import eligible_proofreaders
        free_work = Q(pk__in=[])
        if eligible_proofreaders().filter(pk=user.pk).exists():
            free_work = Q(can_produce_audio=True, audiobook__status=Audiobook.Status.PROOFREADING,
                audiobook__proofreader__isnull=True) & (Q(has_finished_audio_correction=False)
                    | Q(audiobook__active_stage__stage_type=Audiobook.Status.PROOFREADING,
                        audiobook__active_stage__is_completed=False))
        texts = texts.filter(free_work | Q(own_audio_correction=True)
            | Q(audiobook__proofreader=user, audiobook__status=Audiobook.Status.PROOFREADING)
            | Q(has_audio_correction=False, audiobook__proofreader=user, audiobook__proofreading_started_at__isnull=False))
    if hide_completed:
        finished = history.filter(is_completed=True)
        unfinished = history.filter(is_completed=False)
        if not coordinator:
            finished = finished.filter(performer=user)
            unfinished = unfinished.filter(Q(text__audiobook__proofreader=user)
                | Q(text__audiobook__proofreader__isnull=True), pk=OuterRef('audiobook__active_stage_id'))
        texts = texts.annotate(finished_audio_work=Exists(finished), unfinished_audio_work=Exists(unfinished)).exclude(
            finished_audio_work=True, unfinished_audio_work=False)
    return texts.prefetch_related(Prefetch('audiobook_stages', queryset=corrections(), to_attr='audio_corrections'))


def can_assign_proofreader(audio, history):
    return audio.status == Audiobook.Status.PROOFREADING and (
        bool(audio.active_stage_id) or not any(stage.is_completed for stage in history))


def performer_label(user):
    if not user:
        return 'Nie zapisano wykonawcy'
    person = getattr(user, 'person_profile', None)
    return str(person) if person else (user.get_full_name() or user.username)


def correction_rows(audio, history, *, viewer=None):
    rows = []
    for stage in history:
        active = stage.pk == audio.active_stage_id and not stage.is_completed
        # A reassignment changes the ongoing work, never a completed credit.
        performer = audio.proofreader if active else stage.performer
        if viewer is not None and (not performer or performer.pk != viewer.pk):
            continue
        rows.append({'pk': stage.pk, 'performer_id': performer.pk if performer else None,
            'person_id': getattr(getattr(performer, 'person_profile', None), 'pk', None),
            'performer': performer_label(performer), 'started_at': stage.started_at,
            'ended_at': stage.ended_at, 'is_completed': stage.is_completed,
            'is_active': active, 'state': 'Zakończona' if stage.is_completed else 'W toku' if active else 'Nieaktywna'})
    if not history and (viewer is None or audio.proofreader_id == viewer.pk):
        rows.append({'pk': None, 'performer_id': audio.proofreader_id,
            'person_id': getattr(getattr(audio.proofreader, 'person_profile', None), 'pk', None),
            'performer': performer_label(audio.proofreader), 'started_at': audio.proofreading_started_at,
            'ended_at': None, 'is_completed': False, 'is_active': False,
            'state': 'Oczekuje na etap' if audio.status == Audiobook.Status.PROOFREADING else 'Brak szczegółów etapu'})
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
        if not history and audio.status != 'proofreading' and not audio.proofreading_started_at:
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
                'has_reserved_work': stage['state'] == 'Oczekuje na etap', 'state_label': stage['state'],
                'latest_stage': {**stage, 'imported_completed': stage['is_completed']},
                'text': {'pk': text.pk, 'title': text.title, 'is_translation': translated,
                    'anthology': {'pk': text.anthology_id, 'title': text.anthology.title} if text.anthology_id else None,
                    'authors': {'all': [{'pk': a.pk, 'display_name': a.display_name} for a in authors]}}})
    return result
