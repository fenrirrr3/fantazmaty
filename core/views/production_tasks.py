from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from core.permissions import coordinator_required
from core.pagination import paginate_items
from core.table_sorting import DisplayTable
from core.translation_scope import non_abandoned
from texts.models import Anthology, AnthologyTask


@never_cache
@login_required
@require_GET
@coordinator_required
def task_list(request):
    books = non_abandoned(Anthology.objects).exclude(status=Anthology.Status.READY).select_related(
        'cover_illustrator').prefetch_related('production_tasks__assigned_to')
    query = request.GET.get('q', '').strip()[:200]
    choices = [('cover', 'Okładka'), *AnthologyTask.TaskType.choices]
    rows = []
    for book in books:
        tasks = {task.task_type: task for task in book.production_tasks.all()}
        cover_state = {'not_started': 'pending', 'in_progress': 'active', 'ready': 'completed'}
        cells = [{'status': book.get_cover_status_display(), 'date': book.cover_commissioned_at,
                  'state': cover_state[book.cover_status], 'person': book.cover_artist_name}]
        for kind, label in choices[1:]:
            task = tasks.get(kind)
            status = task.status if task else 'not_commissioned'
            cells.append({'status': task.get_status_display() if task else 'Niezlecone',
                          'state': {'not_commissioned': 'pending', 'commissioned': 'active', 'ready': 'completed',
                                    'not_applicable': 'completed'}[status],
                          'date': task.commissioned_at if task else None,
                          'person': str(task.assigned_to) if task and task.assigned_to_id else ''})
        for (kind, label), cell in zip(choices, cells):
            if kind == 'audio_description' and not book.is_novel:
                cell['url'] = reverse('core:audio_description_detail', args=[book.pk])
            else:
                route = 'core:novel_detail' if book.is_novel else 'core:anthology_detail'
                cell['url'] = reverse(route, args=[book.pk]) + ('?tasks=1' if book.is_novel else '') + '#task-' + kind
        searchable = ' '.join([book.title, *(str(c['status']) + ' ' + c['person'] for c in cells)]).casefold()
        if all(term in searchable for term in query.casefold().split()):
            rows.append({'anthology': book, 'anthology_title': book.title, 'cells': cells})
    columns = {'Antologia': ('anthology', lambda row: row['anthology_title'])}
    for index, (kind, label) in enumerate(choices):
        columns[label] = (kind, lambda row, i=index: (
            row['cells'][i]['status'], row['cells'][i]['date'].isoformat() if row['cells'][i]['date'] else ''))
    page = paginate_items(request, DisplayTable(rows, columns))
    return render(request, 'core/production_tasks.html', {'page_obj': page, 'tasks': page,
                   'query': query, 'task_choices': choices})
