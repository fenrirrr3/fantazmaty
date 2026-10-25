"""Wysyłka autorom plików z zakończonego podziału DOCX: formularz → lista kontrolna → wysyłka → raport.

Tylko dla superusera. Adres zawiera podpisany token zadania programu (ten sam co strona postępu),
więc działa wyłącznie w sesji, która uruchomiła podział, i tylko do wygaśnięcia zadania.
"""
from django import forms
from django.contrib import messages
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

TEST_ADDRESS = 'split_mailing_test_address'
TEST_ACTION = 'Test wysyłki plików autorom'
RETRY_ACTION = 'Ponowienie wysyłki plików autorom'


def _no_control_characters(value, label):
    if any((ord(char) < 32 and char not in '\n\r\t') or ord(char) == 127 for char in value):
        raise forms.ValidationError(f'{label}: usuń znaki sterujące.')
    return value


TAGS_HELP = 'Tagi: ' + ', '.join(f'[{tag}] – {label}' for tag, label in mailing.TAGS.items()) + '.'


def _known_tags(value):
    """Nieznany tag (np. literówka „[tytul]”) – błąd z listą dostępnych tagów."""
    unknown = mailing.unknown_tags(value)
    if unknown:
        raise forms.ValidationError(f'Nieznane tagi: {", ".join(unknown)}. Dostępne: '
                                    f'{", ".join(f"[{tag}]" for tag in mailing.TAGS)}.')
    return value


class SplitMailingForm(forms.Form):
    anthology = forms.ModelChoiceField(label='Antologia', queryset=None, empty_label='Wybierz antologię',
                                       help_text='Nagłówki części porównujemy z tytułami tekstów tej antologii, '
                                                 'a w Ekstraktach – z autorami naboru o tej nazwie.')
    subject = forms.CharField(label='Temat wiadomości', max_length=200, help_text=TAGS_HELP)
    message = forms.CharField(label='Treść wiadomości', max_length=5000, widget=forms.Textarea(attrs={'rows': 6}),
                              help_text='Każdy autor dostaje tylko pliki swoich tekstów; tagi jak w temacie '
                                        'uzupełniamy osobno dla każdego maila.')

    def __init__(self, *args, **kwargs):
        from core.translation_scope import non_abandoned
        from texts.models import Anthology

        super().__init__(*args, **kwargs)
        self.fields['anthology'].queryset = non_abandoned(Anthology.objects).order_by('title', 'pk')

    def clean_subject(self):
        subject = self.cleaned_data['subject']
        if any(ord(char) < 32 or ord(char) == 127 for char in subject):
            raise forms.ValidationError('Temat musi mieścić się w jednym wierszu, bez znaków sterujących.')
        return _known_tags(subject)

    def clean_message(self):
        return _known_tags(_no_control_characters(self.cleaned_data['message'], 'Treść'))


class TestMailForm(forms.Form):
    test_address = forms.EmailField(label='Adres testowy', max_length=254,
                                    help_text='Jeden mail z tematem „[TEST] …” i plikiem losowego gotowego tekstu; '
                                              'partia zostaje do właściwej wysyłki.')


def _url(token):
    return reverse('core:split_mailing', args=[token])


def _render(request, context, status=200):
    return render(request, 'core/split_mailing.html', {'status_labels': mailing.STATUS_LABELS, **context},
                  status=status)


def _failed(request, context, batch, error, test_form=None):
    """Strona z błędem wysyłki: lista kontrolna partii albo – bez partii – formularz od nowa."""
    if batch is None:
        return _render(request, {**context, 'form': SplitMailingForm(), 'error': str(error)}, status=400)
    return _render(request, {**context, **_checklist(request, batch, test_form), 'error': str(error)}, status=400)


def _mailbox(context):
    """Skrzynka wysyłkowa albo MailingError z powodem jej braku."""
    if context['mailbox'] is None:
        raise mailing.MailingError(context['mailbox_error'])
    return context['mailbox']


def _test_form(request, test_form=None):
    """Formularz maila testowego z adresem zapamiętanym w sesji."""
    return test_form or TestMailForm(initial={'test_address': request.session.get(TEST_ADDRESS, '')})


def _record_activity(request, action, target):
    """Wpis w dzienniku aktywności: antologia i liczby (test – adres właściciela) – bez treści wiadomości."""
    from core.activity_spool import enqueue_activity

    try:
        enqueue_activity(user_id=request.user.pk, actor=(request.user.email or request.user.get_username())[:254],
                         method='POST', action=action, target=target[:255], path=request.path[:1000],
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
    action = request.POST.get('action') if request.method == 'POST' else None
    try:
        mailbox, mailbox_error = mailing.mailing_mailbox(), ''
    except mailing.MailingError as error:
        mailbox, mailbox_error = None, str(error)
    batch = mailing.read_batch(folder)
    context = {'token': token, 'mailbox': mailbox, 'mailbox_error': mailbox_error}
    if report is not None:
        return _report_page(request, folder, report, batch, context, action)
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
            report = mailing.send(folder, batch, set(request.POST.getlist('rows')), _mailbox(context))
        except mailing.MailingError as error:
            return _failed(request, context, batch, error)
        _record_send(request, LABELS['split_mailing'], report)
        if report['sent']:
            # Raport pokazuje strona partii (PRG): odświeżenie nie wysyła ponownie.
            return redirect(_url(token))
        return _render(request, {**context, **_checklist(request, batch), 'failed_report': report})
    if action == 'test':
        test_form = TestMailForm(request.POST)
        try:
            batch = mailing.checked_batch(folder, request.user, request.POST.get('batch'))
            if not test_form.is_valid():
                return _render(request, {**context, **_checklist(request, batch, test_form)}, status=400)
            _send_test(request, folder, batch, context, test_form)
        except mailing.MailingError as error:
            return _failed(request, context, batch, error, test_form)
        return redirect(_url(token))
    if action not in (None, 'change'):
        return _render(request, {**context, 'error': 'Nieprawidłowa akcja.'}, status=400)
    if batch is None:
        return _render(request, {**context, 'form': SplitMailingForm()})
    return _render(request, {**context, **_checklist(request, batch)})


def _record_send(request, action, report):
    """Wpis w dzienniku dla jednej próby wysyłki (pierwszej albo ponowienia) – liczby tej próby."""
    last = report['last']
    _record_activity(request, action, f'{report["anthology_title"]}: wiadomości {last["mails"]}, '
                                      f'pliki {last["sent"]} z {last["sent"] + last["failed"]}')


def _send_test(request, folder, batch, context, test_form):
    """Mail testowy na adres z formularza; MailingError przy braku skrzynki lub błędzie wysyłki."""
    mailbox = _mailbox(context)
    address = test_form.cleaned_data['test_address']
    request.session[TEST_ADDRESS] = address
    file = mailing.send_test(folder, batch, address, mailbox)
    _record_activity(request, TEST_ACTION, f'{batch.get("anthology_title", "")}: test na {address}')
    messages.success(request, f'Wysłano test na {address} (plik: {file}).')


def _report_page(request, folder, report, batch, context, action):
    """Raport po wysyłce. Gdy część wierszy nie wyszła: ponowienie tylko nieudanych i mail testowy."""
    complete = not report['failed'] or (folder / mailing.SENT).exists()
    token = context['token']

    def page(error='', test_form=None, status=200):
        extra = {}
        if not complete and batch is not None:
            extra = {'batch_token': mailing.batch_token(request.user, batch), 'test_form': _test_form(request, test_form)}
        return _render(request, {**context, **extra, 'report': report, 'complete': complete, 'error': error},
                       status=status)

    if action == 'test' and complete:
        return page('Pliki z tej partii zostały już wysłane – test nie jest już możliwy.', status=400)
    if action not in ('retry', 'test') or complete:
        return page()
    test_form = TestMailForm(request.POST) if action == 'test' else None
    try:
        batch = mailing.checked_batch(folder, request.user, request.POST.get('batch'))
        if action == 'test':
            if not test_form.is_valid():
                return page(test_form=test_form, status=400)
            _send_test(request, folder, batch, context, test_form)
            return redirect(_url(token))
        report = mailing.send(folder, batch, {str(index) for index in mailing.failed_indices(report)},
                              _mailbox(context))
    except mailing.MailingError as error:
        return page(str(error), test_form, status=400)
    _record_send(request, RETRY_ACTION, report)
    last = report['last']
    (messages.success if not last['failed'] else messages.warning)(
        request, f'Ponowiono wysyłkę: pliki {last["sent"]} z {last["sent"] + last["failed"]}.')
    return redirect(_url(token))


def _checklist(request, batch, test_form=None):
    mails = mailing.planned_mails(batch)
    # Numer maila przy każdym jego wierszu, temat – przy pierwszym.
    planned = {row['index']: (number, subject if position == 0 else '')
               for number, group, subject in mails for position, row in enumerate(group)}
    rows = [{**row, **dict(zip(('mail', 'mail_subject'), planned.get(row['index'], (None, ''))))}
            for row in batch['rows']]
    return {'batch': batch, 'rows': rows, 'batch_token': mailing.batch_token(request.user, batch),
            'ready_count': len(planned), 'mail_count': len(mails), 'test_form': _test_form(request, test_form)}
