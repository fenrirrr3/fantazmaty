from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from core.permissions import team_member_required, is_coordinator
from core.pagination import paginate_items
from core.translation_scope import ordinary
from texts.models import Anthology, AnthologyTask


@never_cache
@login_required
@require_GET
@team_member_required
def audio_descriptions(request):
    return render(request, 'core/audio_descriptions.html')


@never_cache
@login_required
@require_GET
@team_member_required
def task_list(request):
    books = ordinary(Anthology.objects).exclude(status=Anthology.Status.READY).prefetch_related('production_tasks__assigned_to')
    if not is_coordinator(request.user):
        books = books.exclude(is_novel=True)
    query = request.GET.get('q', '').strip()[:200]
    selected_type = request.GET.get('task', '')
    choices = [('cover', 'Okładka'), *AnthologyTask.TaskType.choices]
    rows = []
    for book in books:
        rows.append({'anthology': book, 'anthology_title': book.title, 'task':'cover', 'name':'Okładka',
                     'person':book.cover_author, 'status':book.get_cover_status_display(), 'date':None})
        for task in book.production_tasks.all():
            rows.append({'anthology':book, 'anthology_title':book.title, 'task':task.task_type,
                         'name':task.get_task_type_display(), 'person':str(task.assigned_to) if task.assigned_to_id else '',
                         'status':task.get_status_display(), 'date':task.commissioned_at})
    if selected_type:
        rows = [row for row in rows if row['task'] == selected_type]
    if query:
        terms = query.casefold().split()
        rows = [row for row in rows if all(term in ' '.join((row['anthology_title'],row['name'],row['person'],row['status'])).casefold() for term in terms)]
    page = paginate_items(request, rows)
    return render(request, 'core/production_tasks.html', {'page_obj':page, 'tasks':page,
                   'query':query, 'task_choices':choices, 'selected_task':selected_type})
