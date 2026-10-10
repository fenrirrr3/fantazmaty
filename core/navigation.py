"""Return to the exact local list, without storing shared state between tabs."""
from urllib.parse import parse_qs, urlsplit

from django.http import HttpResponseRedirect, QueryDict
from django.urls import Resolver404, resolve
from django.utils.deprecation import MiddlewareMixin

LISTS = {
    'core:home', 'core:text_list', 'core:my_texts', 'core:available_texts',
    'core:workflow_list', 'core:translation_list', 'core:novel_list', 'core:novel_detail',
    'core:person_detail', 'core:author_detail', 'core:anthology_list', 'core:task_list',
    'core:anthology_detail', 'core:audio_descriptions', 'core:audiobooks',
    'core:audio_proofreading', 'core:post_layout', 'core:recruitment_list',
    'core:recruitment_mailbox', 'core:review_list', 'core:my_reviews',
    'core:unlinked_reviews', 'core:extract_list', 'core:tag_list', 'core:global_search',
    'illustrations:illustration_list', 'illustrations:illustrator_list',
}
DETAILS = {
    'core:assigned_text_detail', 'core:translation_detail', 'core:audiobook_detail',
    'core:audio_description_detail', 'core:recruitment_detail', 'core:assigned_review_detail',
    'core:post_layout_edit', 'core:link_text_review', 'core:anthology_detail',
    'core:novel_detail', 'core:chapter_edit', 'core:audio_contributor',
    'illustrations:illustration_detail', 'illustrations:illustrator_edit',
}
PARENTS = {
    'illustration_detail': 'illustration_list', 'audiobook_detail': 'audiobooks',
    'audio_contributor': 'audiobooks', 'recruitment_detail': 'recruitment_list',
    'unlinked_reviews': 'review_list', 'post_layout_edit': 'post_layout',
    'link_text_review': 'review_list', 'chapter_edit': 'novel_list',
}


def local_url(request, value):
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme and parts.scheme not in ('http', 'https'):
            return None
        if parts.netloc and parts.netloc != request.get_host():
            return None
        if not parts.path.startswith('/') or parts.path.startswith('//'):
            return None
        match = resolve(parts.path)
    except (ValueError, Resolver404):
        return None
    return parts, match


def list_url(request, value):
    parsed = local_url(request, value)
    if not parsed or parsed[1].view_name not in LISTS:
        return ''
    parts, _ = parsed
    # A list never needs another return chain. Also bounds nesting to one level.
    query = QueryDict(parts.query).copy()
    query.pop('_back', None)
    return parts.path + ('?' + query.urlencode() if query else '')


def origin(request):
    supplied = request.GET.get('_back') or request.POST.get('_back')
    if supplied:
        return list_url(request, supplied)
    parsed = local_url(request, request.META.get('HTTP_REFERER', ''))
    if not parsed:
        return ''
    parts, match = parsed
    # On a form submission inherit the detail's original list, not the detail itself.
    if request.method == 'GET' and match.view_name in LISTS and parts.path != request.path:
        return list_url(request, parts.path + ('?' + parts.query if parts.query else ''))
    inherited = parse_qs(parts.query).get('_back', [''])[0]
    if match.view_name in DETAILS and inherited:
        return list_url(request, inherited)
    if parts.path == request.path:
        return ''
    return list_url(request, parts.path + ('?' + parts.query if parts.query else ''))


class NavigationMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        request.navigation_return = origin(request)
        match = request.resolver_match
        if (request.method == 'GET' and match.view_name in DETAILS
                and request.navigation_return and '_back' not in request.GET
                and request.navigation_return.split('?')[0] != request.path):
            query = request.GET.copy()
            query['_back'] = request.navigation_return
            return HttpResponseRedirect(request.path + '?' + query.urlencode())

    def process_response(self, request, response):
        back = getattr(request, 'navigation_return', '')
        if not back or response.status_code not in (302, 303):
            return response
        destination = local_url(request, response.get('Location', ''))
        if not destination:
            return response
        parts, match = destination
        if match.view_name in DETAILS:
            query = QueryDict(parts.query).copy()
            query['_back'] = back
            response['Location'] = parts.path + '?' + query.urlencode() + ('#' + parts.fragment if parts.fragment else '')
        elif request.method == 'POST' and match.view_name in LISTS and resolve(urlsplit(back).path).view_name == match.view_name:
            response['Location'] = back
        return response


def context(request):
    match = request.resolver_match
    if not match:
        return {}
    back = getattr(request, 'navigation_return', '')
    current, namespace = match.url_name, match.namespace
    if match.view_name in DETAILS and back:
        parent = resolve(urlsplit(back).path)
        current, namespace = parent.url_name, parent.namespace
    current = PARENTS.get(current, current)
    return {'return_url': back if match.view_name in DETAILS else '',
            'navigation_current': current, 'navigation_namespace': namespace}
