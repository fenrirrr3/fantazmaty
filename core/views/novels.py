from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction, IntegrityError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from core.novel_forms import NovelForm, ChapterForm, ChapterRangeForm, AssignmentForm, CoverForm
from core.pagination import paginate_items
from core.permissions import team_member_required, coordinator_required, is_coordinator
from core.selectors.texts import _annotated_texts
from core.views.supervision import _task_forms
from texts.models import Anthology, NovelProfile, Text
from texts import novels
from texts.vocabulary import canonicalize


@login_required
@require_GET
@team_member_required
def novel_list(request):
    query = Anthology.objects.filter(is_novel=True).exclude(status='abandoned').select_related('novel').prefetch_related('novel__authors').annotate(
        chapter_count=Count('texts'))
    term = request.GET.get('q', '').strip()
    state = request.GET.get('status', '')
    if term:
        author_matches = NovelProfile.objects.filter(Q(authors__pseudonym__plcontains=term) |
                            Q(authors__pseudonym='', authors__first_name__plcontains=term) |
                            Q(authors__pseudonym='', authors__last_name__plcontains=term)).values('anthology_id')
        query = query.filter(Q(title__plcontains=term) | Q(pk__in=author_matches))
    if state in dict(Anthology.Status.choices):
        query = query.filter(status=state)
    sort = request.GET.get('sort', 'title')
    sort = sort if sort in ('title', '-title', 'status', '-status') else 'title'
    page = paginate_items(request, query.order_by(sort, 'pk'))
    return render(request, 'core/novels/list.html', {'page_obj': page, 'q': term, 'selected_status': state,
        'sort': sort, 'statuses': Anthology.Status.choices, 'coordinator': is_coordinator(request.user)})


@login_required
@require_http_methods(['GET', 'POST'])
@coordinator_required
def novel_add(request):
    form = NovelForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                book = Anthology.objects.create(title=form.cleaned_data['title'], is_novel=True)
                profile = form.save(commit=False)
                profile.pk = NovelProfile.objects.get(anthology=book).pk
                profile.anthology = book
                profile.tags = canonicalize(profile.tags, 'tag', register=True)
                profile.genre = canonicalize(profile.genre, 'genre', register=True)
                profile.save()
                form.save_authors()
            return redirect('core:novel_detail', novel_id=book.pk)
        except (IntegrityError, ValidationError):
            form.add_error(None, 'Nie zapisano powieści. Sprawdź dane autora i czy nie dodano go już w bazie.')
    return render(request, 'core/novels/form.html', {'form': form, 'heading': 'Dodaj powieść'}, status=400 if request.method == 'POST' else 200)


@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def novel_detail(request, novel_id):
    book = get_object_or_404(Anthology, pk=novel_id, is_novel=True)
    profile = NovelProfile.objects.filter(anthology=book).first()
    coordinator = is_coordinator(request.user)
    error = None
    forms = {}
    if request.method == 'POST':
        if not coordinator:
            raise PermissionDenied
        try:
            with novels.locked_book(novel_id, request.user, request.POST.get('novel_token', '')) as book:
                profile, _ = NovelProfile.objects.get_or_create(anthology=book)
                action = request.POST.get('action')
                if action == 'reopen':
                    book.status = Anthology.Status.IN_PREPARATION
                    book.save(update_fields=['status'])
                else:
                    novels.require_open(book)
                    if action == 'chapters':
                        form = ChapterRangeForm(request.POST, prefix='add')
                        forms['chapter_range_form'] = form
                        if not form.is_valid():
                            raise ValidationError('Popraw numery rozdziałów lub liczby znaków.')
                        count = novels.add_chapters(book, profile, form.cleaned_data['chapter_numbers'],
                                                   lengths=form.cleaned_data['chapter_lengths'])
                        messages.info(request, f'Dodano rozdziały: {count}. Istniejące numery pozostawiono bez zmian.')
                    elif action == 'chapter':
                        form = ChapterForm(request.POST, instance=Text(anthology=book))
                        forms['chapter_form'] = form
                        if not form.is_valid():
                            raise ValidationError('Popraw dane rozdziału.')
                        novels.new_chapter(book, profile, **form.cleaned_data)
                    elif action == 'assign':
                        assignment_form = AssignmentForm(request.POST, prefix='assign')
                        forms['assignment_form'] = assignment_form
                        raw_ids = request.POST.getlist('chapters')
                        if any(not value.isascii() or not value.isdecimal() or len(value) > 18 for value in raw_ids):
                            raise ValidationError('Nieprawidłowy wybór rozdziałów.')
                        ids = [int(value) for value in raw_ids]
                        forms['selected_chapter_ids'] = ids
                        if not assignment_form.is_valid():
                            raise ValidationError('Wybierz rolę i członka zespołu z uprawnieniami do tej roli.')
                        data = assignment_form.cleaned_data
                        novels.assign_chapters(book, request.user, ids, [(data['role'], data['assignee'])])
                    elif action == 'production':
                        task_forms = _task_forms(book, request.POST)
                        cover_form = CoverForm(request.POST, instance=book)
                        forms.update(task_forms=task_forms, cover_form=cover_form)
                        if not all([f.is_valid() for f in task_forms] + [cover_form.is_valid()]):
                            raise ValidationError('Popraw zadania produkcyjne.')
                        cover_form.save()
                        for form in task_forms:
                            form.save()
                    elif action == 'finish':
                        novels.validate_ready_novel(book)
                        book.status = Anthology.Status.READY
                        book.save(update_fields=['status'])
                    else:
                        raise ValidationError('Nieznana operacja.')
            messages.success(request, 'Zapisano zmiany powieści.')
            return redirect('core:novel_detail', novel_id=novel_id)
        except (ValidationError, IntegrityError) as exc:
            error = ' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Konflikt danych. Sprawdź, czy numer rozdziału jest wolny.'
            book.refresh_from_db()
            profile = NovelProfile.objects.filter(anthology=book).first()
    query = _annotated_texts(include_novels=True, include_translations=True).filter(anthology=book).prefetch_related(
        'workflow_role_assignments__assigned_to__person_profile')
    search = request.GET.get('q', '').strip()
    if search:
        query = query.filter(chapter_number=int(search)) if search.isascii() and search.isdecimal() and len(search) <= 10 else query.none()
    chapters = query.order_by('chapter_number', 'pk')
    from workflow.models import WorkflowStage
    rows = []
    for chapter in chapters:
        rows.append({'chapter': chapter, 'status': dict(WorkflowStage.StageType.choices).get(chapter.current_stage_type, 'Brak etapu'),
                     'assignments': [a for a in chapter.workflow_role_assignments.all() if a.is_current and a.workflow_cycle == chapter.current_workflow_cycle and a.assigned_to_id]})
    context = {'book': book, 'profile': profile, 'rows': rows, 'q': search,
        'coordinator': coordinator, 'error': error, 'production_open': request.GET.get('tasks') == '1' or bool(error and request.POST.get('action') in ('production', 'finish')), 'is_open': book.status != Anthology.Status.READY,
        'novel_token': novels.edit_token(book, request.user) if coordinator else '',
        'chapter_form': ChapterForm(), 'chapter_range_form': ChapterRangeForm(prefix='add'),
        'assignment_form': AssignmentForm(prefix='assign'),
        'task_forms': _task_forms(book) if coordinator else [], 'cover_form': CoverForm(instance=book) if coordinator else None}
    context.update(forms)
    return render(request, 'core/novels/detail.html', context, status=400 if error else 200)


@login_required
@require_http_methods(['GET', 'POST'])
@coordinator_required
def chapter_edit(request, novel_id, chapter_id):
    book = get_object_or_404(Anthology, pk=novel_id, is_novel=True)
    chapter = get_object_or_404(Text, pk=chapter_id, anthology=book)
    form = ChapterForm(request.POST or None, instance=chapter)
    if request.method == 'POST' and form.is_valid():
        try:
            with novels.locked_book(book.pk, request.user, request.POST.get('novel_token', '')) as locked:
                novels.require_open(locked)
                form.instance.full_clean()
                form.save()
            return redirect('core:novel_detail', novel_id=book.pk)
        except (ValidationError, IntegrityError) as exc:
            form.add_error(None, ' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Ten numer rozdziału jest już zajęty.')
    return render(request, 'core/novels/form.html', {'book': book, 'form': form, 'heading': 'Edytuj rozdział',
        'novel_token': novels.edit_token(book, request.user)}, status=400 if request.method == 'POST' else 200)
