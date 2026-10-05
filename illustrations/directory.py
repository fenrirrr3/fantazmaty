"""Coordinator-only directory sharing Person records with illustration assignments."""
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
from people.admin import PersonAdminForm
from people.models import Person, Role


class IllustratorForm(PersonAdminForm):
    version = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Person
        fields = ('first_name', 'last_name', 'email', 'illustrator_portfolio',
                  'illustrator_preferences', 'illustrator_covers', 'illustrator_active')
        widgets = {
            'illustrator_preferences': forms.Textarea(attrs={'rows': 4}),
            'illustrator_portfolio': forms.URLInput(attrs={'placeholder': 'https://...'}),
        }



def directory_rows():
    return Person.objects.filter(roles__name__iexact='Ilustrator', illustrator_active=True).distinct()


def check_access(user):
    if not is_coordinator(user):
        raise PermissionDenied('Spis ilustratorów jest dostępny wyłącznie koordynatorom i superuserom.')


def edit_token(user, person):
    return signing.dumps([user.pk, person.pk, version_of(person)], salt='illustrator-directory')


def matches(token, user, person):
    try:
        return signing.loads(token, salt='illustrator-directory', max_age=86400) == [
            user.pk, person.pk, version_of(person)]
    except signing.BadSignature:
        return False


@never_cache
@login_required
@require_GET
def illustrator_list(request):
    check_access(request.user)
    query = request.GET.get('q', '').strip()[:200]
    rows = directory_rows().order_by('last_name', 'first_name', 'pk')
    if query:
        rows = rows.filter(Q(first_name__icontains=query) | Q(last_name__icontains=query)
                           | Q(email__icontains=query) | Q(illustrator_preferences__icontains=query))
    page = paginate_queryset(request, rows)
    return render(request, 'core/illustrator_list.html', {'page_obj': page, 'query': query})


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
def illustrator_edit(request, illustrator_id=None):
    check_access(request.user)
    with transaction.atomic():
        person = Person()
        if illustrator_id is not None:
            rows = Person.objects.select_for_update() if request.method == 'POST' else Person.objects
            person = get_object_or_404(rows, pk=illustrator_id)
            if not person.roles.filter(name__iexact='Ilustrator').exists():
                from django.http import Http404
                raise Http404
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
                        role = Role.objects.filter(name__iexact='Ilustrator').order_by('pk').first()
                        if role is None:
                            role, _ = Role.objects.get_or_create(name='Ilustrator')
                        person.roles.add(role)
                except IntegrityError:
                    form.add_error('email', 'Nie zapisano danych. Sprawdź, czy ten adres e-mail nie został już dodany.')
                else:
                    messages.success(request, 'Zapisano ilustratora.')
                    return redirect('illustrations:illustrator_list')
        return render(request, 'core/illustrator_form.html', {'form': form, 'person': person}, status=status)
