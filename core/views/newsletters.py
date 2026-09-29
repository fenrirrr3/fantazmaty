from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from core.permissions import superuser_required
from core.models import NewsletterConsent
from core.pagination import paginate_items


@never_cache
@login_required
@require_GET
@superuser_required
def newsletter_list(request):
    rows = NewsletterConsent.objects.order_by('email', 'pk')
    selected = request.GET.get('consent', '')
    filters = {
        'general': {'premieres': True},
        'recruitment': {'recruitment': True},
        'none': {'premieres': False, 'recruitment': False},
    }
    if selected in filters:
        rows = rows.filter(**filters[selected])
    else:
        selected = ''
    page = paginate_items(request, rows)
    return render(request, 'core/newsletter_list.html', {
        'consents': page, 'page_obj': page, 'selected': selected,
    })
