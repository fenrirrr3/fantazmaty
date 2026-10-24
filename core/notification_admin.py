"""Panel admina: reguły powiadomień Discord, role, historia wysyłek i testowe wysyłanie."""
import random

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from authors.admin import SuperuserOnlyAdminMixin
from core.discord_notifications import (ANONYMOUS_AUTHOR, AUDIOBOOK, DISABLED_NOTE, DISCORD_LIMIT, EVENT_COMPLETED,
                                        EVENT_STARTED, REMINDABLE, REMINDER_DAYS, STAGE_EVENTS, WAITING,
                                        configured_channel_names, mention_text, parse_mention, preview_link,
                                        render, rule_problems)
from core.models import DiscordNotification, DiscordRole, NotificationRule, NotificationTemplate
from core.notification_models import TEMPLATE_TAGS

TAGS_HELP = 'Tagi: ' + ', '.join(f'[{tag}] – {label}' for tag, label in TEMPLATE_TAGS.items()) + '.'
COMPLETED_HELP = ('Jedna wiadomość po rozpoczęciu i po zakończeniu każdego etapu (redakcja – także jej przejęcie '
                  'z „Teksty do wzięcia” i wznowienie po weryfikacji – weryfikacje, korekty, kontrole, stylowanie). '
                  '[zdarzenie] – „Rozpoczęto” albo „Zakończono”; etap rozpoczęty i zakończony w jednej operacji – tylko '
                  'zakończenie; sama rezerwacja pierwszej weryfikacji – bez wiadomości. Każde przekazanie redakcji – '
                  'do W1, W2, koordynatora, a w Ekstraktach do W1 albo K1 – [etap] wskazuje cel, np. „Redakcja → '
                  'Pierwsza weryfikacja”. Bez wiadomości: przekazanie pliku autorowi i jego powrót, „Gotowe”, '
                  'wycofanie, uzupełnianie i korekty historii w adminie (brakujący etap, daty, wykonawca, cofnięcie '
                  'zakończenia). [osoba] – wykonawca etapu.')

AUDIOBOOK_HELP = ('Jedna wiadomość po każdej zmianie w produkcji audiobooka: zmiana statusu, rozpoczęcie, przejęcie '
                  'i zakończenie korekty, przypisanie korektora, poprawa dat i wykonawców etapów, usunięcie etapu. '
                  'Bez wiadomości: edycja lektora, montażysty, uwag i publikacji. [zmiana] – opis zmiany, '
                  '[status] – status po zmianie, [osoba] – kto wprowadził zmianę, [link] – strona audiobooka.')
TRIGGER_HELP = {STAGE_EVENTS: COMPLETED_HELP, AUDIOBOOK: AUDIOBOOK_HELP}


def configured_channels():
    from core.discord_webhook import channels, ConfigurationError
    try:
        return list(channels())
    except ConfigurationError:
        return []


class NotificationRuleForm(forms.ModelForm):
    channel = forms.ChoiceField(label='Kanał', required=False)

    class Meta:
        model = NotificationRule
        fields = ('enabled', 'channel', 'mention')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        names = configured_channels()
        current = self.instance.channel
        if current and current not in names:
            names.append(current)
        waiting = self.instance.trigger == WAITING
        empty = '– ten sam co w pierwszym powiadomieniu –' if waiting else '– wybierz kanał –'
        self.fields['channel'].choices = [('', empty)] + [(n, n) for n in names]
        self.fields['channel'].help_text = ('Kanały z konfiguracji webhooków serwera (DISCORD_WEBHOOKS).'
                                            if configured_channels() else
                                            'Na serwerze nie skonfigurowano żadnego webhooka (DISCORD_WEBHOOKS).')
        if waiting:
            self.fields['channel'].help_text += (' Puste – przypomnienie trafia na kanał, na którym ogłoszono tekst '
                                                 '(redakcja, korekta, weryfikacja).')
            self.fields['mention'].help_text = ('Np. @redaktor albo @here. Puste – takie samo oznaczenie jak '
                                                'w pierwszym powiadomieniu o tym tekście.')

    def clean_mention(self):
        value = self.cleaned_data.get('mention', '').strip()
        try:
            parse_mention(value)
        except ValueError as exc:
            raise forms.ValidationError(str(exc))
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('enabled') and not cleaned.get('channel') and self.instance.trigger != WAITING:
            self.add_error('channel', 'Włączone powiadomienie wymaga kanału.')
        return cleaned


class NotificationTemplateInline(admin.StackedInline):
    model = NotificationTemplate
    extra = 1
    fields = ('content',)
    formfield_overrides = {}

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == 'content':
            field.widget = forms.Textarea(attrs={'rows': 3, 'cols': 90})
            field.help_text = 'Tagi: ' + ' '.join(f'[{tag}]' for tag in TEMPLATE_TAGS) + ' – opis tagów i testowa wysyłka na liście powiadomień.'
        return field


@admin.register(DiscordRole)
class DiscordRoleAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('__str__', 'role_id')
    search_fields = ('name',)


@admin.register(NotificationRule)
class NotificationRuleAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = NotificationRuleForm
    inlines = (NotificationTemplateInline,)
    list_display = ('__str__', 'enabled', 'channel', 'mention', 'template_count', 'problems')
    list_display_links = ('__str__',)
    list_editable = ()
    fields = ('trigger_label', 'enabled', 'channel', 'mention')
    readonly_fields = ('trigger_label', 'reminder_targets')
    change_list_template = 'admin/core/notificationrule/change_list.html'

    @admin.display(description='Kiedy')
    def trigger_label(self, obj):
        if obj.trigger in TRIGGER_HELP:
            return format_html('{}<div class="help">{}</div>', obj.get_trigger_display(), TRIGGER_HELP[obj.trigger])
        return obj.get_trigger_display()

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        return (*fields, 'reminder_targets') if obj is not None and obj.trigger == WAITING else fields

    @admin.display(description='Gdzie trafią przypomnienia')
    def reminder_targets(self, obj):
        rules = {rule.trigger: rule for rule in NotificationRule.objects.filter(trigger__in=REMINDABLE)}
        rows = [(rules[value], *reminder_target(obj, rules[value])) for value in REMINDABLE if value in rules]
        return format_html('<ul class="cms-compact-list">{}</ul>', format_html_join(
            '', '<li>po „{}” → kanał: {}, oznaczenie: {}</li>',
            ((base, channel or '– brak (przypomnienie nie wyjdzie) –', mention or '–') for base, channel, mention in rows)))

    @admin.display(description='Treści')
    def template_count(self, obj):
        return len(obj.templates.all())

    @admin.display(description='Uwagi')
    def problems(self, obj):
        found = rule_problems(obj, configured_channel_names())
        return format_html_join('', '<div{}>{}</div>', (
            (format_html(' class="cms-error-text"') if text == DISABLED_NOTE else '', text)
            for text in found)) if found else 'działa'

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('templates')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        custom = [path('test/', self.admin_site.admin_view(self.test_view), name='core_notificationrule_test')]
        return custom + super().get_urls()

    def test_view(self, request):
        if not self.has_view_or_change_permission(request):
            return redirect('admin:index')
        form = TestNotificationForm(request.POST or None)
        rules = {rule.trigger: rule for rule in NotificationRule.objects.prefetch_related('templates')}
        if request.method == 'POST' and form.is_valid():
            reminder_for = request.POST.get('reminder_for', '')
            if reminder_for:
                rule, base = rules.get(WAITING), rules.get(reminder_for) if reminder_for in REMINDABLE else None
            else:
                trigger = request.POST.get('trigger', '')
                rule, base = (rules.get(trigger) if trigger != WAITING else None), None
            if rule is None or (reminder_for and base is None):
                messages.error(request, 'Nieznany rodzaj wiadomości.')
            else:
                level, text = send_test(rule, form.cleaned_data, base)
                messages.add_message(request, level, text)
            return TemplateResponse(request, 'admin/core/notificationrule/test.html', self.test_context(request, form, rules))
        return TemplateResponse(request, 'admin/core/notificationrule/test.html', self.test_context(request, form, rules))

    def test_context(self, request, form, rules):
        def url(value):
            return reverse('admin:core_notificationrule_change', args=[rules[value].pk]) if value in rules else ''
        configured = configured_channel_names()
        roles = {r.name.lower(): r.role_id for r in DiscordRole.objects.all()}
        buttons = [{'trigger': value, 'label': label, 'rule': rules.get(value), 'url': url(value),
                    'problems': rule_problems(rules[value], configured, roles) if value in rules else []}
                   for value, label in NotificationRule.Trigger.choices if value != WAITING]
        waiting = rules.get(WAITING)
        reminders = []
        if waiting is not None:
            for value in REMINDABLE:
                if value in rules:
                    channel, mention = reminder_target(waiting, rules[value])
                    reminders.append({'trigger': value, 'label': rules[value], 'channel': channel, 'mention': mention,
                                      'inherited_channel': not waiting.channel, 'inherited_mention': not waiting.mention})
        return {**self.admin_site.each_context(request), 'title': 'Testowe wysyłanie powiadomień Discord',
                'form': form, 'buttons': buttons, 'reminders': reminders, 'waiting': waiting,
                'waiting_url': url(WAITING), 'opts': self.model._meta, 'tags_help': TAGS_HELP}


class TestNotificationForm(forms.Form):
    title = forms.CharField(label='[tytuł]', max_length=300, initial='Latarnik')
    author = forms.CharField(label='[autor]', max_length=300, initial='Anna Nowak, A.N. Kowal', required=False,
                             help_text=f'Przy zgłoszeniach do recenzji [autor] to zawsze „{ANONYMOUS_AUTHOR}”.')
    anthology = forms.CharField(label='[antologia]', max_length=300, initial='Cienie miasta', required=False)
    genre = forms.CharField(label='[gatunek]', max_length=100, initial='fantasy', required=False)
    length = forms.IntegerField(label='[długość]', min_value=0, max_value=10_000_000, initial=25000, required=False)
    event = forms.ChoiceField(label='[zdarzenie] (dla „Rozpoczęcie i zakończenie etapu”)', required=False,
                              choices=[(EVENT_COMPLETED, EVENT_COMPLETED), (EVENT_STARTED, EVENT_STARTED)],
                              initial=EVENT_COMPLETED)
    stage = forms.CharField(label='[etap] (dla „Rozpoczęcie i zakończenie etapu”)', max_length=100, initial='Redakcja',
                            required=False,
                            help_text='Przy pozostałych wiadomościach [etap] przyjmuje nazwę etapu z przycisku.')
    person = forms.CharField(label='[osoba] (dla „Rozpoczęcie i zakończenie etapu” i „Produkcja audiobooka”)',
                             max_length=200, initial='Jan Kowalski', required=False)
    change = forms.CharField(label='[zmiana] (dla „Produkcja audiobooka”)', max_length=300,
                             initial='Zmieniono status: Montaż.', required=False)
    audio_status = forms.CharField(label='[status] (dla „Produkcja audiobooka”)', max_length=100, initial='Montaż',
                                   required=False)
    days = forms.TypedChoiceField(label='[dni] (dla przypomnień)', choices=[(d, d) for d in REMINDER_DAYS], coerce=int,
                                  initial=REMINDER_DAYS[0], required=False, empty_value=REMINDER_DAYS[0])
    mark_test = forms.BooleanField(label='Dopisz „[TEST]” na początku wiadomości', initial=True, required=False)


STAGE_NAMES = {
    'review_new': 'Recenzja', 'ready_for_editing': 'Do redakcji', 'first_proofreading': 'Pierwsza korekta',
    'third_proofreading': 'Trzecia korekta', 'first_verification': 'Pierwsza weryfikacja',
    'second_verification': 'Druga weryfikacja', 'third_verification': 'Trzecia weryfikacja',
}


def reminder_target(waiting, base):
    """(kanał, oznaczenie) przypomnienia po ogłoszeniu z reguły base – puste pola przypomnienia biorą wartości z base."""
    return (waiting.channel or getattr(base, 'channel', '')), (waiting.mention or getattr(base, 'mention', ''))


def send_test(rule, data, base=None):
    """Wysyła od razu (bez kolejki) wiadomość z reguły – także wyłączonej – i zapisuje ją w historii.

    Dla przypomnienia base to reguła pierwszego powiadomienia (np. pierwsza korekta): daje nazwę etapu
    oraz kanał i oznaczenie, jeśli przypomnienie nie ma własnych.
    """
    from core.discord_webhook import channels, send_message, ConfigurationError, DeliveryError
    templates = [t.content for t in rule.templates.all()]
    channel, mention, note = rule.channel, rule.mention, ''
    if rule.trigger == WAITING:
        base = base or NotificationRule.objects.get(trigger='ready_for_editing')
        channel, mention = reminder_target(rule, base)
        note = f' (przypomnienie po: „{base}”)'
    if not channel:
        return messages.ERROR, f'„{rule}”: wybierz najpierw kanał w ustawieniach tej wiadomości.'
    if not templates:
        return messages.ERROR, f'„{rule}”: dodaj najpierw co najmniej jedną treść wiadomości.'
    try:
        role_ids, here = parse_mention(mention)
    except ValueError as exc:
        return messages.ERROR, f'„{rule}”: {exc}'
    length = data.get('length')
    review = (base.trigger if base else rule.trigger) == 'review_new'
    audiobook = rule.trigger == AUDIOBOOK
    context = {'tytuł': data['title'], 'autor': ANONYMOUS_AUTHOR if review else data.get('author', ''),
               'osoba': data.get('person', '') if rule.trigger in (STAGE_EVENTS, AUDIOBOOK) else '',
               'zdarzenie': (data.get('event') or EVENT_COMPLETED) if rule.trigger == STAGE_EVENTS else '',
               'zmiana': data.get('change', '') if audiobook else '',
               'status': data.get('audio_status', '') if audiobook else '',
               'antologia': data.get('anthology', ''), 'gatunek': data.get('genre', ''),
               'długość': f'{length:,}'.replace(',', ' ') if length is not None else '',
               'etap': ((data.get('stage') or 'Redakcja') if rule.trigger == STAGE_EVENTS
                        else STAGE_NAMES.get(base.trigger if base else rule.trigger, '')),
               'dni': data.get('days') or REMINDER_DAYS[0]}
    # Testowy tekst nie istnieje w bazie – odnośnik prowadzi do pustego podglądu (nr 0).
    link = (preview_link(review_id=0) if review else preview_link(audiobook_id=0) if audiobook
            else preview_link(text_id=0))
    content = render(random.choice(templates), context, mention_text(role_ids, here), link)
    if data.get('mark_test'):
        content = '[TEST] ' + content
        if len(content) > DISCORD_LIMIT:
            body = content.replace(link, '').strip()
            content = f'{body[:DISCORD_LIMIT - len(link) - 1].rstrip()} {link}'
    item = DiscordNotification.objects.create(trigger=rule.trigger, channel=channel, content=content,
                                              role_ids=','.join(role_ids), mention_here=here, status='sending')
    try:
        configured = channels()
        if channel not in configured:
            item.status = 'disabled'
            item.save(update_fields=['status'])
            return messages.ERROR, f'Kanału „{channel}” nie ma w konfiguracji webhooków serwera.'
        item.message_id = send_message(configured[channel], content, role_ids=role_ids, here=here,
                                       suppress_embeds=True)
        item.status = 'sent'
        item.save(update_fields=['status', 'message_id'])
        if not rule.enabled:
            return messages.WARNING, format_html('Wysłano na kanał „{}”{}: {} Uwaga: to powiadomienie jest {}.',
                                                 channel, note, content, DISABLED_NOTE)
        return messages.SUCCESS, format_html('Wysłano na kanał „{}”{}: {}', channel, note, content)
    except (ConfigurationError, DeliveryError) as exc:
        item.status = 'unknown' if getattr(exc, 'uncertain', False) else 'failed'
        item.save(update_fields=['status'])
        return messages.ERROR, f'Nie wysłano: {exc}'


@admin.register(DiscordNotification)
class DiscordNotificationAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('created_at', 'trigger', 'channel', 'status', 'short_content')
    list_filter = ('trigger', 'status', 'channel')
    search_fields = ('content',)
    readonly_fields = tuple(f.name for f in DiscordNotification._meta.fields)

    @admin.display(description='Treść')
    def short_content(self, obj):
        return obj.content[:100]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
