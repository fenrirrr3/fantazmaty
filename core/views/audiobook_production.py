from contextlib import nullcontext

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core import signing
from django.db import transaction
from django.db.models import Q, Value, OuterRef, Subquery, Case, When, CharField
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from authors.models import Author
from core.audiobook_forms import (AudiobookPeopleForm, AudiobookPublicationForm,
    AudiobookStageForm, AudiobookAssignmentForm, eligible_proofreaders)
from core.audiobook_services import require_available, may_finish, start_stage, finish_stage
from core.edit_policy import edit_policy
from core.models import Audiobook, EditRevision
from core.pagination import paginate_items
from core.permissions import is_coordinator, require_coordinator, team_member_required, can_view_audio_proofreading
from core.public_authors import public_name, name_matches
from texts.models import Text, ForeignAuthor
from texts.production import active_production_texts


def audio_texts():
    return Text.objects.select_related('anthology', 'audiobook__proofreader__person_profile', 'translation').prefetch_related(
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


def list_page(request, *, proofreading=False, bound_assignment=None, status_code=200):
    coordinator = is_coordinator(request.user)
    if proofreading and not can_view_audio_proofreading(request.user):
        raise PermissionDenied('Dostęp wymaga roli Korektor audiobooków lub koordynatora.')
    scope = active_production_texts(audio_texts().filter(for_recording=True, audiobook_blacklisted=False))
    if proofreading:
        scope = scope.filter(audiobook__status=Audiobook.Status.PROOFREADING)
        if not coordinator:
            scope = scope.filter(audiobook__proofreader=request.user)
    anthologies = scope.order_by('anthology__title').values('anthology_id', 'anthology__title').distinct()
    q = request.GET.get('q', '').strip()[:200]
    anthology = request.GET.get('anthology', '')
    status = request.GET.get('status', '')
    rows = scope
    if q:
        rows = rows.filter(Q(title__plcontains=q) | Q(anthology__title__plcontains=q)
            | (Q(anthology__is_translated=True) & name_matches(q, 'translation__foreign_authors__'))
            | (~Q(anthology__is_translated=True) & name_matches(q, 'authors__'))
            | Q(audiobook__narrator_name__plcontains=q) | Q(audiobook__engineer_name__plcontains=q)
            | Q(audiobook__proofreader__person_profile__first_name__plcontains=q)
            | Q(audiobook__proofreader__person_profile__last_name__plcontains=q)).distinct()
    if anthology:
        rows = rows.filter(anthology_id=int(anthology)) if anthology.isascii() and anthology.isdecimal() and len(anthology) <= 18 else rows.none()
    rows = rows.annotate(audio_status=Coalesce('audiobook__status', Value(Audiobook.Status.PENDING)))
    if status:
        rows = rows.filter(audio_status=status) if status in Audiobook.Status.values else rows.none()
    local = Author.objects.filter(texts=OuterRef('pk')).annotate(signature=public_name()).order_by('signature')
    foreign = ForeignAuthor.objects.filter(translations__text_id=OuterRef('pk')).annotate(signature=public_name()).order_by('signature')
    rows = rows.annotate(audio_author=Case(When(anthology__is_translated=True, then=Subquery(foreign.values('signature')[:1])),
        default=Subquery(local.values('signature')[:1]), output_field=CharField()))
    page = paginate_items(request, rows)
    page.object_list = [dict(pk=t.pk, title=t.title, anthology=t.anthology,
        authors_display=authors_display(t), audio=getattr(t, 'audiobook', None),
        proofreader_display=proofreader_display(getattr(t, 'audiobook', None))) for t in page.object_list]
    if proofreading and coordinator:
        choices = [(user.pk, str(user.person_profile)) for user in eligible_proofreaders().select_related('person_profile').order_by('last_name', 'first_name', 'pk')]
        choice_ids = {pk for pk, label in choices}
        versions = dict(EditRevision.objects.filter(model_label='texts.text',
            object_id__in=[r['pk'] for r in page.object_list]).values_list('object_id', 'version'))
        for row in page.object_list:
            form = bound_assignment if bound_assignment and bound_assignment.instance.text_id == row['pk'] else AudiobookAssignmentForm(instance=row['audio'], auto_id=f"id_audio_{row['pk']}_%s")
            extra = [(row['audio'].proofreader_id, row['proofreader_display'])] if row['audio'].proofreader_id and row['audio'].proofreader_id not in choice_ids else []
            form.fields['proofreader'].choices = [('', 'Nie przypisano'), *choices, *extra]
            row['assignment_form'] = form
            row['edit_token'] = signing.dumps([request.user.pk, f"texts.text:{row['pk']}", versions.get(row['pk'], 0)], salt='cms-edit-version')
    return render(request, 'core/audio_proofreading.html' if proofreading else 'core/audiobooks.html',
        dict(texts=page, page_obj=page, anthologies=anthologies, can_assign=coordinator,
        query=q, selected_anthology=anthology, selected_status=status, status_choices=Audiobook.Status.choices), status=status_code)


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
            require_available(locked)
        text = get_object_or_404(audio_texts(), pk=text_id)
        audio = getattr(text, 'audiobook', None) or Audiobook(text=text)
        eligible = active_production_texts(Text.objects.filter(pk=text.pk,
            for_recording=True, audiobook_blacklisted=False)).exists()
        coordinator = is_coordinator(request.user)
        editable = eligible and coordinator
        people_form = AudiobookPeopleForm(instance=audio, prefix='people')
        publication_form = AudiobookPublicationForm(instance=audio, prefix='publication')
        stage_form = AudiobookStageForm()
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
                    stage_form = AudiobookStageForm(request.POST)
                    if stage_form.is_valid():
                        start_stage(text_id=text.pk, user=request.user, **stage_form.cleaned_data)
                        return redirect('core:audiobook_detail', text_id=text.pk)
                elif action == 'finish_stage':
                    raw = request.POST.get('stage_id', '')
                    if not raw.isascii() or not raw.isdecimal() or len(raw) > 18:
                        raise ValidationError('Wybierz prawidłowy etap.')
                    finish_stage(text_id=text.pk, stage_id=int(raw), user=request.user)
                    messages.success(request, 'Zakończono etap i zapisano dzisiejszą datę.')
                    return redirect('core:audiobook_detail', text_id=text.pk)
                else:
                    raise ValidationError('Nieznana operacja. Odśwież podgląd.')
            except ValidationError as exc:
                errors = exc.messages
        stages = list(text.audiobook_stages.all())
        return render(request, 'core/audiobook_detail.html', dict(text=text, audio=audio,
            authors_display=authors_display(text), proofreader_display=proofreader_display(audio),
            people_form=people_form, publication_form=publication_form, stage_form=stage_form,
            stages=stages, errors=errors, can_edit=editable, eligible=eligible, can_coordinate=coordinator,
            can_finish=eligible and may_finish(request.user, audio)),
            status=400 if request.method == 'POST' else 200)


def assignment_policy(request, obj, kwargs):
    require_coordinator(request.user)


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
        form = AudiobookAssignmentForm(request.POST, instance=audio, auto_id=f'id_audio_{text.pk}_%s')
        if form.is_valid():
            form.save()
            messages.success(request, 'Zapisano przypisanie korektora audiobooka.')
            return redirect('core:audio_proofreading')
        return list_page(request, proofreading=True, bound_assignment=form, status_code=400)
