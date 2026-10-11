"""Słownik zamian Odkurzacza (reguła „Własne zamiany słownikowe”).

Przegląd: zespół. Zmiany: koordynatorzy i superuser; wyrażenia regularne
wyłącznie superuser. Aktywne pozycje trafiają do każdego zadania Odkurzacza.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Max
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.models import ReplacementEntry
from core.odkurzacz_forms import ReplacementEntryForm
from core.permissions import is_coordinator, is_superuser, team_member_required


def _can_change(user, entry=None):
    if not is_coordinator(user):
        return False
    return entry is None or entry.kind != ReplacementEntry.Kind.PATTERN or is_superuser(user)


def _next_position():
    return (ReplacementEntry.objects.aggregate(value=Max('position'))['value'] or 0) + 10


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def replacement_list(request):
    can_edit, allow_patterns = is_coordinator(request.user), is_superuser(request.user)
    form = None
    if can_edit:
        form = ReplacementEntryForm(request.POST if request.method == 'POST' else None,
                                    allow_patterns=allow_patterns, initial={'position': _next_position()})
    if request.method == 'POST':
        if not can_edit:
            raise PermissionDenied('Słownik zamian mogą zmieniać koordynatorzy i superuser.')
        if form.is_valid():
            entry = form.save()
            messages.success(request, f'Dodano pozycję: {entry}.')
            return redirect(reverse('core:replacement_list') + '#replacement-table')
    entries = list(ReplacementEntry.objects.order_by('position', 'pk'))
    for entry in entries:
        entry.can_change = _can_change(request.user, entry)
    return render(request, 'core/replacements.html', {
        'entries': entries, 'form': form, 'can_edit': can_edit, 'allow_patterns': allow_patterns,
        'active_count': sum(entry.is_active for entry in entries),
    }, status=400 if form is not None and form.is_bound and form.errors else 200)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def replacement_edit(request, pk):
    entry = get_object_or_404(ReplacementEntry, pk=pk)
    if not _can_change(request.user, entry):
        raise PermissionDenied('Tej pozycji nie możesz zmieniać.' if is_coordinator(request.user)
                               else 'Słownik zamian mogą zmieniać koordynatorzy i superuser.')
    allow_patterns = is_superuser(request.user)
    form = ReplacementEntryForm(instance=entry, allow_patterns=allow_patterns)
    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'delete':
            label = str(entry)
            entry.delete()
            messages.success(request, f'Usunięto pozycję: {label}.')
            return redirect(reverse('core:replacement_list') + '#replacement-table')
        if action == 'toggle':
            with transaction.atomic():
                entry = ReplacementEntry.objects.select_for_update().get(pk=entry.pk)
                entry.is_active = not entry.is_active
                entry.save(update_fields=['is_active', 'updated_at'])
            messages.success(request, f'{"Włączono" if entry.is_active else "Wyłączono"} pozycję: {entry}.')
            return redirect(reverse('core:replacement_list') + '#replacement-table')
        if action != 'save':
            return HttpResponseBadRequest('Nieprawidłowa akcja.')
        form = ReplacementEntryForm(request.POST, instance=entry, allow_patterns=allow_patterns)
        if form.is_valid():
            form.save()
            messages.success(request, f'Zapisano pozycję: {entry}.')
            return redirect(reverse('core:replacement_list') + '#replacement-table')
    return render(request, 'core/replacement_edit.html', {'entry': entry, 'form': form},
                  status=400 if form.is_bound and form.errors else 200)
