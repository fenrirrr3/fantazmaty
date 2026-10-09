"""One URL resolution per request, shared by the custom middleware."""
from django.urls import Resolver404, resolve

_MISSING = object()


def resolve_request(request):
    """Return the ResolverMatch for request.path_info, or None for unknown URLs."""
    match = getattr(request, '_cms_resolver_match', _MISSING)
    if match is _MISSING:
        try:
            match = resolve(request.path_info)
        except Resolver404:
            match = None
        request._cms_resolver_match = match
    return match
