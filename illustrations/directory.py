"""Coordinator-only contact directory, independent of accounts and team roles."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from core.edit_versions import version_of
from core.pagination import paginate_queryset
from core.permissions import is_coordinator
from .models import Illustrator


from .contact_forms import IllustratorContactForm


class IllustratorForm(IllustratorContactForm):
    version = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Illustrator
        fields = ('first_name', 'last_name', 'pseudonym', 'email', 'portfolio',
                  'preferences', 'covers', 'is_active')
        widgets = {
            'preferences': forms.Textarea(attrs={'rows': 4}),
            'portfolio': forms.URLInput(attrs={'placeholder': 'https://...'}),
        }



def directory_rows(*, include_inactive=False):
    rows = Illustrator.objects.all()
    return rows if include_inactive else rows.filter(is_active=True)


def check_access(user):
    if not is_coordinator(user):
        raise PermissionDenied('Spis ilustratorów jest dostępny wyłącznie koordynatorom i superuserom.')


def edit_token(user, person):
    return signing.dumps([user.pk, person.pk, version_of(person)], salt='illustrator-contact')


def matches(token, user, person):
    try:
        return signing.loads(token, salt='illustrator-contact', max_age=86400) == [
            user.pk, person.pk, version_of(person)]
    except signing.BadSignature:
        return False


@never_cache
@login_required
@require_GET
def illustrator_list(request):
    check_access(request.user)
    query = request.GET.get('q', '').strip()[:200]
    show_inactive = request.GET.get('show_inactive') == '1'
    rows = directory_rows(include_inactive=show_inactive).annotate(artist_name=Illustrator.display_name_expression()).order_by('artist_name', 'pk')
    if query:
        rows = rows.filter(Q(first_name__plcontains=query) | Q(last_name__plcontains=query)
                           | Q(pseudonym__plcontains=query) | Q(email__icontains=query) | Q(preferences__plcontains=query))
    page = paginate_queryset(request, rows)
    from .contact_forms import duplicate_display_names, contact_label
    duplicates = duplicate_display_names()
    for person in page:
        person.directory_label = contact_label(person, duplicates)
    return render(request, 'core/illustrator_list.html', {'page_obj': page, 'query': query, 'show_inactive': show_inactive})


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
def illustrator_edit(request, illustrator_id=None):
    check_access(request.user)
    with transaction.atomic():
        person = Illustrator()
        if illustrator_id is not None:
            rows = Illustrator.objects.select_for_update() if request.method == 'POST' else Illustrator.objects
            person = get_object_or_404(rows, pk=illustrator_id)
        form = IllustratorForm(request.POST if request.method == 'POST' else None, instance=person,
                               initial={'version': edit_token(request.user, person) if person.pk else ''})
        status = 200
        if request.method == 'POST':
            valid = form.is_valid()
            if person.pk and not matches(request.POST.get('version', ''), request.user, person):
                form.add_error(None, 'Dane zmieniły się lub formularz wygasł. Zachowaj wpisaną treść i odśwież stronę.')
                status = 409
            elif valid:
                try:
                    with transaction.atomic():
                        person = form.save()
                except IntegrityError:
                    form.add_error('email', 'Nie zapisano danych. Sprawdź, czy ten adres e-mail nie został już dodany.')
                else:
                    messages.success(request, 'Zapisano wpis w spisie ilustratorów.')
                    return redirect('illustrations:illustrator_list')
        return render(request, 'core/illustrator_form.html', {'form': form, 'person': person}, status=status)
