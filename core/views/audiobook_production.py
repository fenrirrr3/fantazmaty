from contextlib import nullcontext

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core import signing
from django.db import transaction
from django.db.models import Q, Value, OuterRef, Subquery, Case, When, CharField, DateField, Exists, Prefetch
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from authors.models import Author
from core.audiobook_forms import (AudiobookPeopleForm, AudiobookPublicationForm,
    AudiobookStageForm, AudiobookAssignmentForm, eligible_proofreaders)
from core.audiobook_services import (require_available, may_finish, start_stage, finish_stage, claim_proofreading,
    can_claim_proofreading, allowed_next_stages)
from core.edit_policy import edit_policy
from core.models import Audiobook, AudiobookStage, EditRevision
from core.pagination import paginate_items
from core.permissions import is_coordinator, require_coordinator, team_member_required, can_view_audio_proofreading
from core.public_authors import public_name, name_matches
from texts.models import Text, ForeignAuthor
from texts.production import active_production_texts
from core.selectors.audio_proofreading import proofreading_scope, can_assign_proofreader, correction_rows, corrections


def audio_texts(*, include_abandoned=False):
    scope = Text.objects.all() if include_abandoned else Text.objects.exclude(anthology__status='abandoned')
    return scope.select_related('anthology', 'audiobook__proofreader__person_profile', 'translation').prefetch_related(
        'authors', 'translation__foreign_authors').exclude(anthology__is_novel=True)


def authors_display(text):
    if text.anthology_id and text.anthology.is_translated:
        translation = getattr(text, 'translation', None)
        return ', '.join(person.display_name for person in translation.foreign_authors.all()) if translation else ''
    return text.authors_display


def proofreader_display(audio):
    if not audio or not audio.proofreader_id:
        return ''
    user = audio.proofreader
    profile = getattr(user, 'person_profile', None)
    return str(profile) if profile else (user.get_full_name() or user.username)


def author_links(text, user):
    translated = bool(text.anthology_id and text.anthology.is_translated)
    translation = getattr(text, 'translation', None) if translated else None
    people = translation.foreign_authors.all() if translation else ([] if translated else text.authors.all())
    allowed = user.is_superuser if translated else is_coordinator(user)
    return [{'name': person.display_name, 'url': (
        reverse('core:translation_person_detail', args=['author', person.pk]) if translated else
        reverse('core:author_detail', args=[person.pk])) if allowed else ''} for person in people]


DATE_STAGES = ('recording', 'proofreading', 'corrections', 'editing')
WAITING = 'waiting_next'
STATUS_FILTERS = [*Audiobook.Status.choices[:-1], (WAITING, 'Czeka na kolejny etap'), Audiobook.Status.choices[-1]]


def stage_periods(text):
    audio = getattr(text, 'audiobook', None)
    periods = {kind: [] for kind in DATE_STAGES}
    for stage in text.production_periods:
        periods[stage.stage_type].append({'started_at': stage.started_at, 'ended_at': stage.ended_at})
    for kind in DATE_STAGES:
        if not periods[kind] and audio and getattr(audio, f'{kind}_started_at'):
            periods[kind].append({'started_at': getattr(audio, f'{kind}_started_at'), 'ended_at': None})
    return periods


def list_page(request, *, proofreading=False, bound_assignment=None, status_code=200):
    coordinator = is_coordinator(request.user)
    hide_completed = request.GET.get('hide_completed', '1' if proofreading else '0') == '1'
    if proofreading and not can_view_audio_proofreading(request.user):
        raise PermissionDenied('Dostęp wymaga roli Korektor audiobooków lub koordynatora.')
    scope = active_production_texts(audio_texts().filter(for_recording=True, audiobook_blacklisted=False))
    if proofreading:
        scope = proofreading_scope(audio_texts(), request.user, coordinator, hide_completed=hide_completed)
    anthologies = scope.order_by('anthology__title').values('anthology_id', 'anthology__title').distinct()
    q = request.GET.get('q', '').strip()[:200]
    anthologies_selected = list(dict.fromkeys(v for v in request.GET.getlist('anthology') if v))
    statuses_selected = list(dict.fromkeys(v for v in request.GET.getlist('status') if v))
    rows = scope
    if q:
        rows = rows.filter(Q(title__plcontains=q) | Q(anthology__title__plcontains=q)
            | (Q(anthology__is_translated=True) & name_matches(q, 'translation__foreign_authors__'))
            | (~Q(anthology__is_translated=True) & name_matches(q, 'authors__'))
            | Q(audiobook__narrator_name__plcontains=q) | Q(audiobook__engineer_name__plcontains=q)
            | Q(audiobook__proofreader__person_profile__first_name__plcontains=q)
            | Q(audiobook__proofreader__person_profile__last_name__plcontains=q)).distinct()
        if proofreading:
            # Match historical performers without multiplying the story rows.
            matches = corrections().filter(Q(performer__person_profile__first_name__plcontains=q)
                | Q(performer__person_profile__last_name__plcontains=q))
            if not coordinator:
                matches = matches.filter(performer=request.user)
            rows = scope.filter(Q(pk__in=rows.values('pk')) | Q(pk__in=matches.values('text_id')))
    if anthologies_selected:
        valid = all(v.isascii() and v.isdecimal() and len(v) <= 18 for v in anthologies_selected)
        rows = rows.filter(anthology_id__in=[int(v) for v in anthologies_selected]) if valid else rows.none()
    rows = rows.annotate(audio_status=Coalesce('audiobook__status', Value(Audiobook.Status.PENDING)))
    if statuses_selected:
        if all(v in Audiobook.Status.values or v == WAITING for v in statuses_selected):
            condition = Q(audio_status__in=[v for v in statuses_selected if v != WAITING])
            if WAITING in statuses_selected:
                condition |= (Q(audiobook__active_stage__isnull=True)
                              & ~Q(audio_status__in=(Audiobook.Status.PENDING, Audiobook.Status.PUBLISHED)))
            rows = rows.filter(condition)
        else:
            rows = rows.none()
    if not proofreading:
        rows = rows.prefetch_related(Prefetch('audiobook_stages',
            queryset=AudiobookStage.objects.filter(stage_type__in=DATE_STAGES).order_by('pk'),
            to_attr='production_periods'))
        for kind in DATE_STAGES:
            history = AudiobookStage.objects.filter(text_id=OuterRef('pk'), stage_type=kind)
            rows = rows.alias(**{f'audio_{kind}_date': Case(When(Exists(history),
                then=Subquery(history.filter(started_at__isnull=False).order_by('started_at', 'pk').values('started_at')[:1])),
                default=f'audiobook__{kind}_started_at', output_field=DateField())})
    local = Author.objects.filter(texts=OuterRef('pk')).annotate(signature=public_name()).order_by('signature')
    foreign = ForeignAuthor.objects.filter(translations__text_id=OuterRef('pk')).annotate(signature=public_name()).order_by('signature')
    rows = rows.annotate(audio_author=Case(When(anthology__is_translated=True, then=Subquery(foreign.values('signature')[:1])),
        default=Subquery(local.values('signature')[:1]), output_field=CharField()))
    page = paginate_items(request, rows)
    claimant = proofreading and eligible_proofreaders().filter(pk=request.user.pk).exists()
    page.object_list = [dict(pk=t.pk, title=t.title, anthology=t.anthology,
        authors_display=authors_display(t), author_links=author_links(t, request.user), audio=getattr(t, 'audiobook', None),
        proofreader_display=proofreader_display(getattr(t, 'audiobook', None)),
        **({'corrections': correction_rows(t.audiobook, t.audio_corrections, viewer=None if coordinator else request.user),
            'allow_assignment': coordinator and t.can_produce_audio and can_assign_proofreader(t.audiobook, t.audio_corrections),
            'can_finish': t.can_produce_audio and may_finish(request.user, t.audiobook) and any(
                stage.pk == t.audiobook.active_stage_id and not stage.is_completed for stage in t.audio_corrections),
            'can_claim': claimant and t.can_produce_audio and can_claim_proofreading(t.audiobook, t.audio_corrections, request.user),
            'production_disabled': not t.can_produce_audio} if proofreading else {'periods': stage_periods(t)})) for t in page.object_list]
    if proofreading:
        versions = dict(EditRevision.objects.filter(model_label='texts.text',
            object_id__in=[r['pk'] for r in page.object_list]).values_list('object_id', 'version'))
        for row in page.object_list:
            if row['can_claim'] or row['can_finish'] or (coordinator and row['allow_assignment']):
                row['edit_token'] = signing.dumps([request.user.pk, f"texts.text:{row['pk']}", versions.get(row['pk'], 0)], salt='cms-edit-version')
    if proofreading and coordinator:
        choices = [(user.pk, str(user.person_profile)) for user in eligible_proofreaders().select_related('person_profile').order_by('last_name', 'first_name', 'pk')]
        choice_ids = {pk for pk, label in choices}
        for row in page.object_list:
            if not row['allow_assignment']:
                continue
            form = bound_assignment if bound_assignment and bound_assignment.instance.text_id == row['pk'] else AudiobookAssignmentForm(instance=row['audio'], auto_id=f"id_audio_{row['pk']}_%s")
            extra = [(row['audio'].proofreader_id, row['proofreader_display'])] if row['audio'].proofreader_id and row['audio'].proofreader_id not in choice_ids else []
            form.fields['proofreader'].choices = [('', 'Nie przypisano'), *choices, *extra]
            row['assignment_form'] = form
    return render(request, 'core/audio_proofreading.html' if proofreading else 'core/audiobooks.html',
        dict(texts=page, page_obj=page, anthologies=anthologies, can_assign=coordinator, can_claim=claimant,
        query=q, selected_anthology=request.GET.get('anthology', ''), selected_status=request.GET.get('status', ''),
        selected_anthologies=anthologies_selected, selected_statuses=statuses_selected, status_choices=STATUS_FILTERS,
        hide_completed=hide_completed), status=status_code)


def can_edit_audio(request, obj, kwargs):
    if request.POST.get('action') == 'finish_stage' and may_finish(request.user, Audiobook.objects.filter(text=obj).first()):
        return
    require_coordinator(request.user)


@edit_policy(check=can_edit_audio, require_version=True)
@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def audiobook_detail(request, text_id):
    with transaction.atomic() if request.method == 'POST' else nullcontext():
        if request.method == 'POST':
            locked = get_object_or_404(Text.objects.select_for_update(), pk=text_id)
            can_edit_audio(request, locked, {})
            if request.POST.get('action') not in ('people', 'publication'):
                require_available(locked)
            elif not Audiobook.objects.filter(text=locked).exists():
                require_available(locked)
        text = get_object_or_404(audio_texts(include_abandoned=True).filter(
            ~Q(anthology__status="abandoned") | Q(audiobook__isnull=False)), pk=text_id)
        audio = getattr(text, 'audiobook', None) or Audiobook(text=text)
        eligible = active_production_texts(Text.objects.filter(pk=text.pk,
            for_recording=True, audiobook_blacklisted=False)).exists()
        coordinator = is_coordinator(request.user)
        editable = coordinator and (eligible or audio.pk is not None)
        people_form = AudiobookPeopleForm(instance=audio, prefix='people')
        publication_form = AudiobookPublicationForm(instance=audio, prefix='publication')
        allowed = allowed_next_stages(text, audio) if audio.pk else allowed_next_stages(text)
        stage_form = AudiobookStageForm(allowed=allowed)
        errors = []
        if request.method == 'POST':
            action = request.POST.get('action')
            try:
                if action == 'people':
                    people_form = AudiobookPeopleForm(request.POST, instance=audio, prefix='people')
                    if people_form.is_valid():
                        people_form.save()
                        return redirect('core:audiobook_detail', text_id=text.pk)
                elif action == 'publication':
                    publication_form = AudiobookPublicationForm(request.POST, instance=audio, prefix='publication')
                    if publication_form.is_valid():
                        publication_form.save()
                        return redirect('core:audiobook_detail', text_id=text.pk)
                elif action == 'start_stage':
                    stage_form = AudiobookStageForm(request.POST, allowed=allowed)
                    if stage_form.is_valid():
                        start_stage(text_id=text.pk, user=request.user, **stage_form.cleaned_data)
                        return redirect('core:audiobook_detail', text_id=text.pk)
                elif action == 'finish_stage':
                    raw = request.POST.get('stage_id', '')
                    if not raw.isascii() or not raw.isdecimal() or len(raw) > 18:
                        raise ValidationError('Wybierz prawidłowy etap.')
                    finish_stage(text_id=text.pk, stage_id=int(raw), user=request.user)
                    messages.success(request, 'Zakończono etap i zapisano dzisiejszą datę.')
                    if request.POST.get('return_to') == 'audio_proofreading':
                        return redirect('core:audio_proofreading')
                    return redirect('core:audiobook_detail', text_id=text.pk)
                else:
                    raise ValidationError('Nieznana operacja. Odśwież podgląd.')
            except ValidationError as exc:
                errors = exc.messages
        stages = list(text.audiobook_stages.select_related('performer__person_profile'))
        return render(request, 'core/audiobook_detail.html', dict(text=text, audio=audio,
            authors_display=authors_display(text), proofreader_display=proofreader_display(audio),
            people_form=people_form, publication_form=publication_form, stage_form=stage_form,
            stages=stages, errors=errors, can_edit=editable, can_manage_stages=eligible and coordinator and bool(allowed),
            is_published=audio.status == Audiobook.Status.PUBLISHED, eligible=eligible, can_coordinate=coordinator,
            can_finish=eligible and may_finish(request.user, audio)),
            status=400 if request.method == 'POST' else 200)


def assignment_policy(request, obj, kwargs):
    require_coordinator(request.user)


def claim_policy(request, obj, kwargs):
    if not eligible_proofreaders().filter(pk=request.user.pk).exists():
        raise PermissionDenied('Przejęcie wymaga aktywnej roli Korektor audiobooków.')


@edit_policy(check=claim_policy, require_version=True)
@never_cache
@login_required
@require_POST
@team_member_required
def claim_audio_proofreading(request, text_id):
    claim_policy(request, None, {})
    get_object_or_404(Text, pk=text_id)
    try:
        claim_proofreading(text_id=text_id, user=request.user)
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    else:
        messages.success(request, 'Przejęto korektę audiobooka.')
    return redirect('core:audio_proofreading')


@edit_policy(check=assignment_policy, require_version=True)
@never_cache
@login_required
@require_POST
@team_member_required
def assign_audio_proofreader(request, text_id):
    require_coordinator(request.user)
    with transaction.atomic():
        text = get_object_or_404(Text.objects.select_for_update(), pk=text_id)
        require_available(text)
        audio = get_object_or_404(Audiobook, text=text, status=Audiobook.Status.PROOFREADING)
        if not can_assign_proofreader(audio, corrections().filter(text=text)):
            raise PermissionDenied('Korekta została zakończona. Aby przypisać kolejną osobę, rozpocznij nowy etap Korekta.')
        form = AudiobookAssignmentForm(request.POST, instance=audio, auto_id=f'id_audio_{text.pk}_%s')
        if form.is_valid():
            form.save()
            messages.success(request, 'Zapisano przypisanie korektora audiobooka.')
            return redirect('core:audio_proofreading')
        return list_page(request, proofreading=True, bound_assignment=form, status_code=400)


@never_cache
@login_required
@require_GET
@team_member_required
def audio_proofreading(request):
    return list_page(request, proofreading=True)
