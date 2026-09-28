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
    selected = {}
    for field in ('premieres', 'recruitment'):
        value = request.GET.get(field, '')
        selected[field] = value if value in ('yes', 'no') else ''
        if selected[field]:
            rows = rows.filter(**{field: value == 'yes'})
    page = paginate_items(request, rows)
    return render(request, 'core/newsletter_list.html', {
        'consents': page, 'page_obj': page, 'selected': selected,
    })
