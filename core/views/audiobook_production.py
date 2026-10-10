from contextlib import nullcontext

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, Value, OuterRef, Subquery, Case, When, CharField
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from authors.models import Author
from core.audiobook_forms import AudiobookProductionForm
from core.edit_policy import edit_policy
from core.models import Audiobook
from core.pagination import paginate_items
from core.permissions import is_coordinator, require_coordinator, team_member_required
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


def list_page(request):
    scope = active_production_texts(audio_texts().filter(for_recording=True, audiobook_blacklisted=False))
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
    return render(request, 'core/audiobooks.html', dict(texts=page, page_obj=page, anthologies=anthologies,
        query=q, selected_anthology=anthology, selected_status=status, status_choices=Audiobook.Status.choices))


def can_edit_audio(request, obj, kwargs):
    require_coordinator(request.user)


@edit_policy(check=can_edit_audio, require_version=True)
@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def audiobook_detail(request, text_id):
    with transaction.atomic() if request.method == 'POST' else nullcontext():
        # The Text row serializes first creation, edits and blacklist changes.
        if request.method == 'POST':
            require_coordinator(request.user)
            get_object_or_404(Text.objects.select_for_update(), pk=text_id)
        text = get_object_or_404(audio_texts(), pk=text_id)
        audio = getattr(text, 'audiobook', None) or Audiobook(text=text)
        eligible = active_production_texts(Text.objects.filter(pk=text.pk,
            for_recording=True, audiobook_blacklisted=False)).exists()
        editable = eligible and is_coordinator(request.user)
        if request.method == 'POST' and not editable:
            raise PermissionDenied('Tekst nie jest dostępny do produkcji audiobooka. Zachowano dotychczasowe dane.')
        form = AudiobookProductionForm(request.POST if request.method == 'POST' else None, instance=audio)
        if request.method == 'POST' and form.is_valid():
            form.save()
            messages.success(request, 'Zapisano przypisanie i status audiobooka.')
            return redirect('core:audiobook_detail', text_id=text.pk)
        groups = [
            ('Status audiobooka', ['status']),
            ('Lektor', ['narrator_name', 'narrator_email', 'recording_started_at', 'corrections_started_at']),
            ('Korektor audiobooka', ['proofreader', 'proofreading_started_at']),
            ('Dźwiękowiec', ['engineer_name', 'engineer_email', 'editing_started_at']),
            ('Publikacja', ['awaiting_publication_started_at', 'premiere_date', 'youtube_url', 'hearthis_url']),
        ]
        if not editable:
            for field in form.fields.values():
                field.disabled = True
            form.fields['proofreader'].queryset = form.fields['proofreader'].queryset.filter(pk=audio.proofreader_id)
        if not is_coordinator(request.user):
            groups = [(label, [name for name in names if not name.endswith('_email')]) for label, names in groups]
        return render(request, 'core/audiobook_detail.html', dict(text=text, audio=audio,
            authors_display=authors_display(text), form=form, can_edit=editable, eligible=eligible,
            field_groups=[(label, [form[name] for name in names]) for label, names in groups]),
            status=400 if request.method == 'POST' else 200)
