"""Public illustration catalogue: an explicit projection without contact data."""

from django import forms
from django.core import signing
from django.db.models import Exists, OuterRef, Q, Case, When, Value, CharField
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.pagination import paginate_queryset
from core.translation_scope import ordinary
from texts.models import Anthology, Text
from texts.production import active_production_texts
from .validators import validate_drive_url


class PublicLinkForm(forms.Form):
    drive_url = forms.URLField(label='Wspólny link GDrive', required=False, max_length=1000,
        validators=[validate_drive_url], widget=forms.URLInput(attrs={'placeholder': 'https://drive.google.com/…'}))
    version = forms.CharField(widget=forms.HiddenInput)


def link_version(user, url):
    return signing.dumps([user.pk, url], salt='public-illustrations-link')


def valid_link_version(user, url, token):
    try:
        return signing.loads(token, salt='public-illustrations-link', max_age=86400) == [user.pk, url]
    except (signing.BadSignature, TypeError):
        return False


def visible_illustrations():
    from .models import Illustration
    return ordinary(Illustration.objects).filter(
        text_id__in=active_production_texts(Text.objects.all()).values('pk'),
        text__anthology__has_illustrations=True,
    ).exclude(text__anthology__status=Anthology.Status.READY)


@never_cache
@require_GET
def external_illustrations(request):
    from .models import Illustration, PublicIllustrationSettings
    assigned = Illustration.illustrators.through.objects.filter(illustration_id=OuterRef('pk'))
    rows = visible_illustrations().annotate(has_artist=Exists(assigned)).annotate(
        public_status=Case(When(Q(has_artist=True) | ~Q(manual_illustrator_name=''), then=Value('Przypisane')),
                           default=Value('Dostępne'), output_field=CharField()))
    anthology = request.GET.get('anthology', '')
    choices = rows.values('text__anthology_id', 'text__anthology__title').order_by('text__anthology__title').distinct()
    if anthology:
        rows = rows.filter(text__anthology_id=int(anthology)) if anthology.isascii() and anthology.isdecimal() and len(anthology) <= 18 else rows.none()
    status = request.GET.get('status', '')
    if status in ('Dostępne', 'Przypisane'):
        rows = rows.filter(public_status=status)
    query = request.GET.get('q', '').strip()[:200]
    if query:
        rows = rows.filter(Q(text__title__plcontains=query) | Q(text__tags__plcontains=query) |
                           Q(text__genre__plcontains=query) | Q(text__anthology__title__plcontains=query))
    # No private model instances or related profiles in the public context.
    rows = rows.order_by('text__anthology__title', 'text__title', 'pk').values(
        'text__anthology__title', 'text__title', 'text__tags', 'text__genre', 'public_status')
    page = paginate_queryset(request, rows)
    config = PublicIllustrationSettings.objects.filter(pk=1).first()
    return render(request, 'core/external_illustrations.html', {
        'rows': page, 'page_obj': page, 'anthologies': choices, 'selected_anthology': anthology,
        'selected_status': status, 'query': query,
        'drive_url': config.drive_url if config else '',
    })
