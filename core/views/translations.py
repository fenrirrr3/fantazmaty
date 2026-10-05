from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from core.permissions import team_member_required, superuser_required
from core.pagination import paginate_items
from core.selectors.texts import text_list_context
from core.translation_forms import TranslationForm
from core.views.texts import _permission_context, _render_text_detail
from texts.models import Text, TextTranslation


@never_cache
@login_required
@require_GET
@superuser_required
def translation_person_detail(request, kind, person_id):
    from django.http import Http404
    from texts.models import ForeignAuthor, Translator
    models = {'author': ForeignAuthor, 'translator': Translator}
    if kind not in models:
        raise Http404
    person = get_object_or_404(models[kind], pk=person_id)
    records = person.translations.filter(text__anthology__is_translated=True).select_related('text__anthology')
    return render(request, 'core/translation_person_detail.html', {
        'person': person, 'kind': kind, 'records': records,
        'admin_change_name': 'admin:texts_translator_change' if kind == 'translator' else 'admin:texts_foreignauthor_change',
    })


@never_cache
@login_required
@require_GET
@team_member_required
def translation_list(request):
    context = dict(text_list_context(user=request.user, params=request.GET, translated=True))
    page = paginate_items(request, context.pop('texts'))
    context.update(_permission_context(request.user))
    context.update(texts=page, page_obj=page, translations_page=True, list_url_name='core:translation_list')
    return render(request, 'core/text_list.html', context)


@never_cache
@login_required
@require_GET
@team_member_required
def translation_detail(request, text_id):
    text = get_object_or_404(Text.objects.select_related('anthology'), pk=text_id, anthology__is_translated=True)
    return _render_text_detail(request, text)


@never_cache
@login_required
@require_POST
@superuser_required
def set_translators(request, text_id):
    with transaction.atomic():
        text = get_object_or_404(Text.objects.select_for_update().select_related('anthology'),
                                 pk=text_id, anthology__is_translated=True)
        record, _ = TextTranslation.objects.get_or_create(text=text)
        form = TranslationForm(request.POST, instance=record)
        if not form.is_valid():
            return _render_text_detail(request, text, bound_forms={'translation_form':form}, status=400)
        form.save()
        messages.success(request, 'Zapisano autora zagranicznego i tłumacza tekstu.')
    return redirect('core:translation_detail', text_id=text.pk)
