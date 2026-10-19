"""Wysyłka autorom plików z zakończonego podziału DOCX: formularz → lista kontrolna → wysyłka → raport.

Tylko dla superusera. Adres zawiera podpisany token zadania programu (ten sam co strona postępu),
więc działa wyłącznie w sesji, która uruchomiła podział, i tylko do wygaśnięcia zadania.
"""
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.activity import LABELS
from core.permissions import is_superuser, team_member_required
from core.services import program_jobs as jobs
from core.services import split_mailing as mailing



def _no_control_characters(value, label):
    if any((ord(char) < 32 and char not in '\n\r\t') or ord(char) == 127 for char in value):
        raise forms.ValidationError(f'{label}: usuń znaki sterujące.')
    return value


class SplitMailingForm(forms.Form):
    anthology = forms.ModelChoiceField(label='Antologia', queryset=None, empty_label='Wybierz antologię',
                                       help_text='Nagłówki części porównujemy z tytułami tekstów tej antologii.')
    subject = forms.CharField(label='Temat wiadomości', max_length=200)
    message = forms.CharField(label='Treść wiadomości', max_length=5000, widget=forms.Textarea(attrs={'rows': 6}),
                              help_text='Ta sama treść trafi do każdego autora; w załączniku tylko plik jego tekstu.')

    def __init__(self, *args, **kwargs):
        from core.translation_scope import non_abandoned
        from texts.models import Anthology

        super().__init__(*args, **kwargs)
        self.fields['anthology'].queryset = non_abandoned(Anthology.objects).order_by('title', 'pk')

    def clean_subject(self):
        subject = self.cleaned_data['subject']
        if any(ord(char) < 32 or ord(char) == 127 for char in subject):
            raise forms.ValidationError('Temat musi mieścić się w jednym wierszu, bez znaków sterujących.')
        return subject

    def clean_message(self):
        return _no_control_characters(self.cleaned_data['message'], 'Treść')


def _url(token):
    return reverse('core:split_mailing', args=[token])


def _render(request, context, status=200):
    return render(request, 'core/split_mailing.html', {'status_labels': mailing.STATUS_LABELS, **context},
                  status=status)


def _record_activity(request, report):
    """Wpis w dzienniku aktywności: antologia i liczby – bez adresów i treści wiadomości."""
    from core.activity_spool import enqueue_activity

    target = f'{report["anthology_title"]}: wysłano {report["sent"]} z {report["sent"] + report["failed"]}'
    try:
        enqueue_activity(user_id=request.user.pk, actor=(request.user.email or request.user.get_username())[:254],
                         method='POST', action=LABELS['split_mailing'], target=target[:255], path=request.path[:1000],
                         status_code=200)
    except OSError:
        import logging
        logging.getLogger(__name__).exception('Nie udało się zapisać wysyłki plików w dzienniku aktywności.')


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@team_member_required
def split_mailing(request, token):
    if not is_superuser(request.user):
        raise PermissionDenied('Wysyłka plików autorom jest dostępna tylko dla superusera.')
    try:
        folder = jobs.resolve(request, token)
        state = jobs.status(folder)
    except jobs.JobError as error:
        return _render(request, {'error': str(error)}, status=404)
    except (OSError, ValueError):
        return _render(request, {'error': 'Nie można odczytać zadania. Spróbuj ponownie.'}, status=503)
    if state.get('state') != 'done' or not state.get('split'):
        return _render(request, {'error': 'Wysyłka autorom jest dostępna po zakończonym podziale DOCX.'}, status=404)
    report = mailing.read_report(folder)
    if report is not None:
        return _render(request, {'report': report, 'token': token})
    try:
        mailbox, mailbox_error = mailing.mailing_mailbox(), ''
    except mailing.MailingError as error:
        mailbox, mailbox_error = None, str(error)
    batch = mailing.read_batch(folder)
    action = request.POST.get('action') if request.method == 'POST' else None
    context = {'token': token, 'mailbox': mailbox, 'mailbox_error': mailbox_error}
    if action == 'prepare':
        form = SplitMailingForm(request.POST)
        if not form.is_valid():
            return _render(request, {**context, 'form': form}, status=400)
        try:
            mailing.prepare(folder, form.cleaned_data['anthology'],
                            form.cleaned_data['subject'], form.cleaned_data['message'])
        except mailing.MailingError as error:
            return _render(request, {**context, 'form': form, 'error': str(error)}, status=400)
        # Nowy podpis zadania: lista kontrolna ma pełny czas ważności.
        return redirect(_url(jobs.refreshed_token(token)))
    if action == 'change' and batch is not None:
        (folder / mailing.BATCH).unlink(missing_ok=True)
        return _render(request, {**context, 'form': SplitMailingForm(initial={
            'anthology': batch.get('anthology'), 'subject': batch.get('subject'), 'message': batch.get('message')})})
    if action == 'send':
        try:
            batch = mailing.checked_batch(folder, request.user, request.POST.get('batch'))
            if mailbox is None:
                raise mailing.MailingError(mailbox_error)
            report = mailing.send(folder, batch, set(request.POST.getlist('rows')), mailbox)
        except mailing.MailingError as error:
            if batch is None:
                return _render(request, {**context, 'form': SplitMailingForm(), 'error': str(error)}, status=400)
            return _render(request, {**context, **_checklist(request, batch), 'error': str(error)}, status=400)
        _record_activity(request, report)
        if report['sent']:
            # Raport pokazuje strona partii (PRG): odświeżenie nie wysyła ponownie.
            return redirect(_url(token))
        return _render(request, {**context, **_checklist(request, batch), 'failed_report': report})
    if action not in (None, 'change'):
        return _render(request, {**context, 'error': 'Nieprawidłowa akcja.'}, status=400)
    if batch is None:
        return _render(request, {**context, 'form': SplitMailingForm()})
    return _render(request, {**context, **_checklist(request, batch)})


def _checklist(request, batch):
    return {'batch': batch, 'batch_token': mailing.batch_token(request.user, batch),
            'ready_count': sum(row.get('status') == mailing.READY for row in batch['rows'])}
