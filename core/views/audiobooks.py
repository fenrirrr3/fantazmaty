from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from core.pagination import paginate_items
from core.permissions import team_member_required
from core.selectors.texts import text_list_context
from texts.models import Text


class AudiobookForm(forms.ModelForm):
    class Meta:
        model = Text
        fields = ('for_recording',)


@never_cache
@login_required
@require_GET
@team_member_required
def audiobook_list(request):
    params = request.GET.copy()
    params['hide_ready'] = '0'
    context = dict(text_list_context(user=request.user, params=params,
        scope=Text.objects.filter(for_recording=True), include_translations=True))
    page = paginate_items(request, context.pop('texts'))
    context.update(texts=page, page_obj=page)
    return render(request, 'core/audiobooks.html', context)


@never_cache
@login_required
@require_POST
@team_member_required
def update_text_audiobook(request, text_id):
    from core.views.texts import _render_text_detail
    with transaction.atomic():
        text = get_object_or_404(Text.objects.select_for_update(), pk=text_id)
        form = AudiobookForm(request.POST, instance=text)
        if form.is_valid():
            text.save(update_fields=['for_recording'])
            messages.success(request, 'Zapisano ustawienie audiobooka.')
            return redirect('core:assigned_text_detail', text_id=text.pk)
        return _render_text_detail(request, text, bound_forms={'audiobook_form': form}, status=400)
