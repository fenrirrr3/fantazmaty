from collections import Counter

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from core.pagination import paginate_items
from core.table_sorting import DisplayTable
from core.permissions import team_member_required, coordinator_required, is_coordinator
from texts.models import VocabularyTerm, Text, NovelProfile
from texts.catalog_models import term_key
from texts.vocabulary import tokens, refresh_dictionary, merge_plan, merge_token, apply_merge


class TermForm(forms.ModelForm):
    class Meta:
        model = VocabularyTerm
        fields = ('kind', 'name')


@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def vocabulary_list(request):
    form = TermForm(request.POST or None)
    error = None
    if request.method == 'POST':
        if not is_coordinator(request.user):
            raise PermissionDenied
        try:
            with transaction.atomic():
                if request.POST.get('action') == 'refresh':
                    count = refresh_dictionary()
                    messages.success(request, f'Dodano brakujące hasła: {count}. Nie zmieniono treści pól.')
                elif form.is_valid():
                    form.save()
                    messages.success(request, 'Dodano hasło słownika.')
                else:
                    raise ValidationError('Popraw nazwę i rodzaj hasła.')
            return redirect('core:vocabulary_list')
        except (ValidationError, IntegrityError) as exc:
            error = ' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Takie hasło już istnieje.'
    counts = Counter()
    # A novel is counted once, its chapters inherit the same metadata.
    for model, query in [(Text, Text.objects.exclude(anthology__is_novel=True)), (NovelProfile, NovelProfile.objects.all())]:
        for tags, genre in query.values_list('tags', 'genre').iterator():
            for kind, value in [('tag', tags), ('genre', genre)]:
                counts.update((kind, term_key(name)) for name in tokens(value))
    query = VocabularyTerm.objects.select_related('canonical').prefetch_related('aliases')
    kind = request.GET.get('kind', '')
    search = request.GET.get('q', '').strip()
    if kind in dict(VocabularyTerm.Kind.choices):
        query = query.filter(kind=kind)
    if search:
        query = query.filter(name__plcontains=search)
    page = paginate_items(request, DisplayTable(query.order_by('kind', 'name', 'pk'), {
        'Rodzaj': ('kind', lambda term: term.get_kind_display()),
        'Nazwa': ('name', lambda term: term.name),
        'Nazwa docelowa / aliasy': ('aliases', lambda term: term.canonical.name if term.canonical_id else ', '.join(alias.name for alias in term.aliases.all()) or 'Nazwa główna'),
        'Użycia': ('usage', lambda term: counts[(term.kind, term.key)]),
    }))
    for term in page:
        term.usage = counts[(term.kind, term.key)]
    return render(request, 'core/vocabulary/list.html', {'page_obj': page, 'form': form, 'error': error,
        'coordinator': is_coordinator(request.user), 'kinds': VocabularyTerm.Kind.choices, 'kind': kind, 'q': search},
        status=400 if error else 200)


@login_required
@require_GET
@team_member_required
def vocabulary_suggestions(request):
    kind = request.GET.get('kind', '')
    if kind not in dict(VocabularyTerm.Kind.choices):
        return JsonResponse({'results': []})
    search = request.GET.get('q', '').strip()[:255]
    query = VocabularyTerm.objects.filter(kind=kind).select_related('canonical')
    if search:
        query = query.filter(name__plcontains=search)
    names = list(dict.fromkeys((term.canonical or term).name for term in query.order_by('name', 'pk')[:40]))[:12]
    return JsonResponse({'results': names})


@login_required
@require_http_methods(['GET', 'POST'])
@coordinator_required
def vocabulary_merge(request, term_id):
    source = get_object_or_404(VocabularyTerm, pk=term_id)
    target = None
    changes = None
    token = ''
    error = None
    value = request.POST.get('target') if request.method == 'POST' else request.GET.get('target')
    if value and value.isascii() and value.isdecimal() and len(value) < 19:
        target = VocabularyTerm.objects.filter(pk=int(value), kind=source.kind, canonical__isnull=True).exclude(pk=source.pk).first()
    if request.method == 'POST' or target:
        try:
            if not target:
                raise ValidationError('Wybierz nazwę docelową.')
            if request.POST.get('action') == 'apply':
                count = apply_merge(source.pk, target.pk, request.user, request.POST.get('merge_token', ''))
                messages.success(request, f'Ujednolicono {count} rekordów. Poprzednia nazwa pozostaje aliasem.')
                return redirect('core:vocabulary_list')
            changes, digest = merge_plan(source, target)
            token = merge_token(source, target, request.user, digest)
        except ValidationError as exc:
            error = ' '.join(exc.messages)
    titles = {obj.pk: obj.title for obj in Text.objects.filter(pk__in=[c['id'] for c in changes or [] if c['model'] == 'texts.text'])}
    novels = {obj.pk: obj.anthology.title for obj in NovelProfile.objects.select_related('anthology').filter(pk__in=[c['id'] for c in changes or [] if c['model'] == 'texts.novelprofile'])}
    for change in changes or []:
        change['title'] = (titles if change['model'] == 'texts.text' else novels)[change['id']]
    return render(request, 'core/vocabulary/merge.html', {'source': source, 'target': target, 'changes': changes,
        'change_page': paginate_items(request, changes) if changes is not None else None,
        'merge_token': token, 'error': error, 'targets': VocabularyTerm.objects.filter(kind=source.kind, canonical__isnull=True).exclude(pk=source.pk)},
        status=400 if error else 200)
