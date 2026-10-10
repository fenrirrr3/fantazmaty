from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from core.models import AudioContributor, Audiobook
from core.permissions import coordinator_required, team_member_required, is_coordinator


class ContributorForm(forms.ModelForm):
    class Meta:
        model = AudioContributor
        fields = ('name', 'email')
        help_texts = {'name': 'Zmiana dotyczy wszystkich audiobooków tej osoby.'}

    def clean(self):
        from core.audiobook_models import contact_key
        data = super().clean()
        name, email = ' '.join(data.get('name', '').split()), (data.get('email') or '').strip().lower()
        others = AudioContributor.objects.exclude(pk=self.instance.pk)
        if email and others.filter(email__iexact=email).exists():
            self.add_error('email', 'Ten adres ma już inny profil. Połącz profile w panelu admina.')
        if not email and any(contact_key(p.name) == contact_key(name) and not p.email for p in others.only('name', 'email')):
            self.add_error('name', 'Istnieje już profil o tym imieniu i nazwisku bez e-maila. Połącz profile w panelu admina.')
        return data


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def audio_contributor(request, pk):
    from core.views.audiobook_production import authors_display
    person = get_object_or_404(AudioContributor.objects.select_related('user__person_profile'), pk=pk)
    coordinator = is_coordinator(request.user)
    form = ContributorForm(instance=person) if coordinator else None
    if request.method == 'POST':
        if not coordinator:
            raise PermissionDenied('Dane kontaktu zmienia koordynator.')
        with transaction.atomic():
            person = get_object_or_404(AudioContributor.objects.select_for_update(), pk=pk)
            form = ContributorForm(request.POST, instance=person)
            if form.is_valid():
                form.save()
                messages.success(request, 'Zapisano profil. Dane zaktualizowano we wszystkich audiobookach tej osoby.')
                return redirect('core:audio_contributor', pk=person.pk)
    works = list(Audiobook.objects.filter(Q(narrator_contact=person) | Q(engineer_contact=person))
        .select_related('text__anthology', 'text__translation', 'active_stage').prefetch_related(
            'text__authors', 'text__translation__foreign_authors').order_by('text__anthology__title', 'text__title', 'pk'))
    for audio in works:
        audio.author_display = authors_display(audio.text)
    published = [audio for audio in works if audio.status == Audiobook.Status.PUBLISHED]
    current = [audio for audio in works if audio.status != Audiobook.Status.PUBLISHED]
    return render(request, 'core/audio_contributor.html', {'person': person, 'works': published,
        'current_works': current, 'show_email': coordinator, 'form': form},
        status=400 if request.method == 'POST' else 200)


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
        from illustrations.models import Illustrator, Illustration
        rows = Illustrator.objects.filter(is_active=True).filter(Q(first_name__plcontains=query) |
            Q(last_name__plcontains=query) | Q(pseudonym__plcontains=query) | Q(email__icontains=query))[:25]
        results = [{'id': r.pk, 'name': r.display_name, 'email': r.email or ''} for r in rows]
        # Preserve the ability to find previously entered independent contacts.
        manual = Illustration.objects.exclude(manual_illustrator_name='').filter(
            Q(manual_illustrator_name__plcontains=query) | Q(manual_illustrator_email__icontains=query)
        ).values_list('manual_illustrator_name', 'manual_illustrator_email').distinct()[:25]
        inactive = {(r.display_name.casefold(), (r.email or '').casefold()) for r in Illustrator.objects.filter(is_active=False)}
        seen = {(r['name'].casefold(), r['email'].casefold()) for r in results} | inactive
        for name, email in manual:
            if (name.casefold(), email.casefold()) not in seen:
                results.append({'id': None, 'name': name, 'email': email})
                seen.add((name.casefold(), email.casefold()))
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
