from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.core import signing
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from core.edit_versions import version_of
from core.models import AudioContributor, Audiobook
from core.pagination import paginate_queryset
from core.permissions import coordinator_required, team_member_required, is_coordinator


class ContributorForm(forms.ModelForm):
    version = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = AudioContributor
        fields = ('name', 'email', 'is_active')
        help_texts = {'name': 'Zmiana dotyczy wszystkich audiobooków tej osoby.'}

    def clean(self):
        from core.audiobook_models import contact_key
        data = super().clean()
        name, email = ' '.join(data.get('name', '').split()), (data.get('email') or '').strip().lower()
        others = AudioContributor.objects.exclude(pk=self.instance.pk)
        if email and others.filter(email__iexact=email).exists():
            self.add_error('email', 'Ten adres ma już inny wpis. Połącz wpisy w panelu admina.')
        if not email and any(contact_key(p.name) == contact_key(name) and not p.email for p in others.only('name', 'email')):
            self.add_error('name', 'Istnieje już wpis o tym imieniu i nazwisku bez e-maila. Połącz wpisy w panelu admina.')
        return data


def edit_token(user, person):
    return signing.dumps([user.pk, person.pk, version_of(person)], salt='audio-contact')


def token_matches(token, user, person):
    try:
        return signing.loads(token, salt='audio-contact', max_age=86400) == [user.pk, person.pk, version_of(person)]
    except signing.BadSignature:
        return False


@never_cache
@login_required
@require_GET
@coordinator_required
def audio_contributor_list(request):
    """Spis lektorów i montażystów – jak spis ilustratorów, niezależny od kont i ról zespołu."""
    query = request.GET.get('q', '').strip()[:200]
    show_inactive = request.GET.get('show_inactive') == '1'
    rows = AudioContributor.objects.all() if show_inactive else AudioContributor.objects.filter(is_active=True)
    if query:
        rows = rows.filter(Q(name__plcontains=query) | Q(email__icontains=query))
    rows = rows.annotate(narrated=Count('narrated_books', distinct=True),
                         engineered=Count('engineered_books', distinct=True)).order_by('name', 'pk')
    return render(request, 'core/audio_contributor_list.html', {
        'page_obj': paginate_queryset(request, rows), 'query': query, 'show_inactive': show_inactive})


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def audio_contributor(request, pk=None):
    from core.views.audiobook_production import authors_display, require_audiobook_access
    require_audiobook_access(request.user)
    coordinator = is_coordinator(request.user)
    if pk is None and not coordinator:
        raise PermissionDenied('Wpisy spisu dodaje koordynator.')
    person = get_object_or_404(AudioContributor, pk=pk) if pk is not None else AudioContributor()
    form = ContributorForm(instance=person, initial={'version': edit_token(request.user, person) if person.pk else ''}) \
        if coordinator else None
    status = 200
    if request.method == 'POST':
        if not coordinator:
            raise PermissionDenied('Dane wpisu zmienia koordynator.')
        with transaction.atomic():
            if person.pk:
                person = get_object_or_404(AudioContributor.objects.select_for_update(), pk=pk)
            form = ContributorForm(request.POST, instance=person)
            status = 400
            valid = form.is_valid()
            if person.pk and not token_matches(request.POST.get('version', ''), request.user, person):
                form.add_error(None, 'Dane zmieniły się lub formularz wygasł. Zachowaj wpisaną treść i odśwież stronę.')
                status = 409
            elif valid:
                person = form.save()
                messages.success(request, 'Zapisano wpis. Dane zaktualizowano we wszystkich audiobookach tej osoby.')
                return redirect('core:audio_contributor', pk=person.pk)
    works = list(Audiobook.objects.filter(Q(narrator_contact=person) | Q(engineer_contact=person))
        .select_related('text__anthology', 'text__translation', 'active_stage').prefetch_related(
            'text__authors', 'text__translation__foreign_authors').order_by('text__anthology__title', 'text__title', 'pk')) \
        if person.pk else []
    for audio in works:
        audio.author_display = authors_display(audio.text)
    published = [audio for audio in works if audio.is_published]
    current = [audio for audio in works if not audio.is_published]
    return render(request, 'core/audio_contributor.html', {'person': person, 'works': published,
        'current_works': current, 'show_email': coordinator, 'form': form}, status=status)


@never_cache
@login_required
@require_GET
@coordinator_required
def contact_suggestions(request):
    query = request.GET.get('q', '').strip()[:255]
    kind = request.GET.get('kind')
    if len(query) < 2:
        return JsonResponse({'results': []})
    if kind == 'audio':
        rows = AudioContributor.objects.filter(Q(name__plcontains=query) | Q(email__icontains=query)).order_by('name', 'pk')[:25]
        results = [{'id': r.pk, 'name': r.name, 'email': r.email} for r in rows]
    elif kind == 'illustration':
        from illustrations.models import Illustrator
        rows = Illustrator.objects.filter(is_active=True).filter(Q(first_name__plcontains=query) |
            Q(last_name__plcontains=query) | Q(pseudonym__plcontains=query) | Q(email__icontains=query))[:25]
        results = [{'id': r.pk, 'name': r.display_name, 'email': r.email or ''} for r in rows]
    elif kind in ('person', 'cover'):
        # Hints for entries without an account: names only, never contact data.
        terms = query.split()
        if kind == 'person':
            from people.models import Person
            rows = Person.objects.all()
            for term in terms:
                rows = rows.filter(Q(first_name__plcontains=term) | Q(last_name__plcontains=term))
            results = [{'id': r.pk, 'name': str(r), 'email': '',
                        'note': 'zewnętrzny' if r.is_external else ('' if r.is_active else 'nieaktywny')}
                       for r in rows.order_by('last_name', 'first_name', 'pk')[:10]]
        else:
            from illustrations.models import Illustrator
            rows = Illustrator.objects.all()
            for term in terms:
                rows = rows.filter(Q(first_name__plcontains=term) | Q(last_name__plcontains=term) | Q(pseudonym__plcontains=term))
            results = [{'id': r.pk, 'name': r.display_name, 'email': '', 'note': '' if r.is_active else 'nieaktywny'}
                       for r in rows.order_by('last_name', 'first_name', 'pk')[:10]]
    else:
        return JsonResponse({'results': []}, status=400)
    return JsonResponse({'results': results[:25]})
