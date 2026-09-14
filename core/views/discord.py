from core.models import WorkflowEvent
import uuid
from django import forms
from django.db import transaction
from django.views.decorators.debug import sensitive_variables, sensitive_post_parameters
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from core.permissions import superuser_required
from core.models import DiscordDispatch
from core.discord_webhook import channels, send_message, ConfigurationError, DeliveryError

SALT = 'core.discord-test.v1'

class DiscordForm(forms.Form):
    channel = forms.ChoiceField(label='Kanał Discorda')
    content = forms.CharField(label='Wiadomość', max_length=2000, widget=forms.Textarea(attrs={'rows': 6, 'maxlength': 2000}), help_text='Do 2000 znaków. Wzmianki nie wysyłają powiadomień.')
    token = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, channel_names=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['channel'].choices = [(name, name) for name in channel_names]

@transaction.non_atomic_requests
@sensitive_variables()
@sensitive_post_parameters("content", "token")
@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
def discord_test(request):
    error = ''
    try:
        configured = channels()
    except ConfigurationError as exc:
        configured = {}; error = str(exc)
    token = signing.dumps({'user':request.user.pk, 'nonce':str(uuid.uuid4())}, salt=SALT)
    form = DiscordForm(request.POST if request.method == 'POST' else None, channel_names=configured, initial={'token':token})
    if request.method == 'POST' and form.is_valid() and configured:
        try:
            payload = signing.loads(form.cleaned_data['token'], salt=SALT, max_age=3600)
            if payload['user'] != request.user.pk:
                raise ValueError()
            nonce = uuid.UUID(payload['nonce'])
        except (signing.BadSignature, ValueError, KeyError, TypeError):
            form.add_error(None, 'Formularz wygasł albo jest nieprawidłowy. Odśwież stronę i spróbuj ponownie.')
        else:
            dispatch, created = DiscordDispatch.objects.get_or_create(token=nonce, defaults={'user':request.user, 'channel':form.cleaned_data['channel']})
            if not created:
                messages.warning(request, 'Ten formularz był już wysłany. Nie wysłano kolejnej wiadomości; sprawdź wynik poniżej.')
                return redirect('core:discord_test')
            try:
                dispatch.message_id = send_message(configured[form.cleaned_data['channel']], form.cleaned_data['content'])
            except DeliveryError as exc:
                dispatch.status = 'unknown' if exc.uncertain else 'failed'
                messages.error(request, str(exc))
            else:
                dispatch.status = 'sent'
                messages.success(request, f'Wysłano wiadomość na kanał: {dispatch.channel}.')
            dispatch.save(update_fields=['status','message_id'])
            return redirect('core:discord_test')
    return render(request, 'core/discord_test.html', {
        'form':form, 'configured':bool(configured), 'configuration_error':error,
        'history':DiscordDispatch.objects.select_related('user')[:20],
        'workflow_history': WorkflowEvent.objects.all()[:30],
    }, status=400 if request.method == 'POST' else 200)
