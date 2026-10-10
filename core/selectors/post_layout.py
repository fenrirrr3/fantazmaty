from django.urls import reverse

from core.models import PostLayoutAssignment


def profile_post_layout_assignments(person):
    if not person.user_id:
        return []
    result = []
    for item in PostLayoutAssignment.objects.filter(proofreader_id=person.user_id).select_related('anthology'):
        completed = item.status == item.Status.COMPLETED
        result.append({'pk': item.pk, 'kind': 'Poskładowa', 'kind_key': 'post_layout',
            'detail_url': reverse('core:post_layout') + f'?assignment={item.pk}#post-layout-{item.pk}',
            'role': 'post_layout_proofreader', 'get_role_display': 'Korektor poskładowy',
            'assigned_at': None if item.historical else item.created_at, 'has_active_work': item.status == item.Status.IN_PROGRESS,
            'has_reserved_work': item.status == item.Status.ASSIGNED, 'has_completed_work': completed,
            'state_label': item.get_status_display(), 'page_from': item.page_from, 'page_to': item.page_to,
            'latest_stage': {'started_at': item.work_start or item.assigned_start, 'ended_at': item.completed_on},
            'text': {'title': str(item),
                'anthology': {'pk': item.anthology_id, 'title': item.anthology.title}, 'authors': {'all': []}}})
    return result
