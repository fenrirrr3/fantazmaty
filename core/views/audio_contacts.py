from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.models import AudioContributor, Audiobook
from core.permissions import coordinator_required, team_member_required, is_coordinator


@never_cache
@login_required
@require_GET
@team_member_required
def audio_contributor(request, pk):
    from core.views.audiobook_production import authors_display
    person = get_object_or_404(AudioContributor.objects.select_related('user__person_profile'), pk=pk)
    works = list(Audiobook.objects.filter(Q(narrator_contact=person) | Q(engineer_contact=person),
        status=Audiobook.Status.PUBLISHED).select_related('text__anthology', 'text__translation').prefetch_related(
            'text__authors', 'text__translation__foreign_authors'))
    for audio in works:
        audio.author_display = authors_display(audio.text)
    return render(request, 'core/audio_contributor.html', {'person': person, 'works': works,
        'show_email': is_coordinator(request.user)})


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
    else:
        return JsonResponse({'results': []}, status=400)
    return JsonResponse({'results': results[:25]})
