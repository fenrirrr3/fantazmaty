"""Panel admina: reguły powiadomień Discord, role (także rangi nadawane przez CMS – podgląd, synchronizacja,
dopasowanie osób, historia), link zapraszający, historia wysyłek i testowe wysyłanie."""
import random

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from authors.admin import SuperuserOnlyAdminMixin
from core.discord_notifications import (ANONYMOUS_AUTHOR, AUDIOBOOK, DISABLED_NOTE, DISCORD_LIMIT, EVENT_COMPLETED,
                                        EVENT_STARTED, REMINDABLE, REMINDER_DAYS, REVIEW, SOCIAL_CALENDAR,
                                        STAGE_EVENTS, WAITING, calendar_context, calendar_link,
                                        configured_channel_names, discord_roles, mention_text, parse_mention,
                                        preview_link, render, rule_problems, send_item, with_link)
from core.social_models import SocialCalendarEntry
from core.models import DiscordNotification, DiscordRole, NotificationRule, NotificationTemplate
from core.admin_access import ReadOnlyAdminMixin, SingleRecordAdminMixin
from core.notification_models import TEMPLATE_TAGS, DiscordRoleChange, DiscordSettings
from workflow.models import WorkflowStage

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
SOCIAL_CALENDAR_HELP = ('Jedna wiadomość po dodaniu wpisu do kalendarza Social Media (bez wiadomości: edycja '
                        'i usunięcie wpisu). [data] – dzień wpisu, [opis] – co idzie, [platforma] – gdzie, '
                        '[osoba] – kto dodał wpis, [link] – ten dzień w kalendarzu.')
TRIGGER_HELP = {STAGE_EVENTS: COMPLETED_HELP, AUDIOBOOK: AUDIOBOOK_HELP, SOCIAL_CALENDAR: SOCIAL_CALENDAR_HELP}


class NotificationRuleForm(forms.ModelForm):
    channel = forms.ChoiceField(label='Kanał', required=False)

    class Meta:
        model = NotificationRule
        fields = ('enabled', 'channel', 'mention')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configured = configured_channel_names() or []
        names = list(configured)
        current = self.instance.channel
        if current and current not in names:
            names.append(current)
        waiting = self.instance.trigger == WAITING
        empty = '– ten sam co w pierwszym powiadomieniu –' if waiting else '– wybierz kanał –'
        self.fields['channel'].choices = [('', empty)] + [(n, n) for n in names]
        self.fields['channel'].help_text = ('Kanały z konfiguracji webhooków serwera (DISCORD_WEBHOOKS).'
                                            if configured else
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

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == 'content':
            field.widget = forms.Textarea(attrs={'rows': 3, 'cols': 90})
            field.help_text = 'Tagi: ' + ' '.join(f'[{tag}]' for tag in TEMPLATE_TAGS) + ' – opis tagów i testowa wysyłka na liście powiadomień.'
        return field


@admin.register(NotificationRule)
class NotificationRuleAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = NotificationRuleForm
    inlines = (NotificationTemplateInline,)
    list_display = ('__str__', 'enabled', 'channel', 'mention', 'template_count', 'problems')
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

    def test_context(self, request, form, rules):
        def url(value):
            return reverse('admin:core_notificationrule_change', args=[rules[value].pk]) if value in rules else ''
        configured = configured_channel_names()
        roles = discord_roles()
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
    person = forms.CharField(label='[osoba] (dla „Rozpoczęcie i zakończenie etapu”, „Produkcja audiobooka” '
                                   'i kalendarza Social Media)',
                             max_length=200, initial='Jan Kowalski', required=False)
    change = forms.CharField(label='[zmiana] (dla „Produkcja audiobooka”)', max_length=300,
                             initial='Zmieniono status: Montaż.', required=False)
    audio_status = forms.CharField(label='[status] (dla „Produkcja audiobooka”)', max_length=100, initial='Montaż',
                                   required=False)
    calendar_day = forms.DateField(label='[data] (dla kalendarza Social Media)', required=False,
                                   widget=forms.DateInput(attrs={'type': 'date'}),
                                   help_text='Puste – dzisiejsza data.')
    calendar_description = forms.CharField(label='[opis] (dla kalendarza Social Media)', max_length=300,
                                           initial='Cytat z „Latarnika”', required=False)
    calendar_platform = forms.ChoiceField(label='[platforma] (dla kalendarza Social Media)', required=False,
                                          choices=SocialCalendarEntry.Platform.choices,
                                          initial=SocialCalendarEntry.Platform.FACEBOOK)
    days = forms.TypedChoiceField(label='[dni] (dla przypomnień)', choices=[(d, d) for d in REMINDER_DAYS], coerce=int,
                                  initial=REMINDER_DAYS[0], required=False, empty_value=REMINDER_DAYS[0])
    mark_test = forms.BooleanField(label='Dopisz „[TEST]” na początku wiadomości', initial=True, required=False)


# [etap] wiadomości testowej: nazwa etapu z reguły ogłoszenia (reguły etapów nazywają się jak rodzaj etapu).
STAGE_NAMES = {REVIEW: 'Recenzja', **dict(WorkflowStage.StageType.choices)}


def reminder_target(waiting, base):
    """(kanał, oznaczenie) przypomnienia po ogłoszeniu z reguły base – puste pola przypomnienia biorą wartości z base."""
    return (waiting.channel or getattr(base, 'channel', '')), (waiting.mention or getattr(base, 'mention', ''))


def send_test(rule, data, base=None):
    """Wysyła od razu (bez kolejki) wiadomość z reguły – także wyłączonej – i zapisuje ją w historii.

    Dla przypomnienia base to reguła pierwszego powiadomienia (np. pierwsza korekta): daje nazwę etapu
    oraz kanał i oznaczenie, jeśli przypomnienie nie ma własnych.
    """
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
    if rule.trigger == SOCIAL_CALENDAR:
        # Wpis kalendarza nie dotyczy tekstu – jak przy zwykłym zdarzeniu tylko tagi kalendarza i odnośnik do dnia.
        day = data.get('calendar_day') or timezone.localdate()
        platform = data.get('calendar_platform') or SocialCalendarEntry.Platform.FACEBOOK
        context = calendar_context(day=day, description=data.get('calendar_description', ''),
                                   platform=SocialCalendarEntry.Platform(platform).label,
                                   person=data.get('person', ''))
        link = calendar_link(day)
    content = render(random.choice(templates), context, mention_text(role_ids, here), link)
    if data.get('mark_test'):
        content = '[TEST] ' + content
        if len(content) > DISCORD_LIMIT:
            content = with_link(content.replace(link, '').strip(), link)
    item = DiscordNotification.objects.create(trigger=rule.trigger, channel=channel, content=content,
                                              role_ids=','.join(role_ids), mention_here=here, status='sending')
    item.status, item.message_id, error = send_item(item)
    item.save(update_fields=['status', 'message_id'])
    if item.status == 'disabled':
        return messages.ERROR, f'Kanału „{channel}” nie ma w konfiguracji webhooków serwera.'
    if error:
        return messages.ERROR, f'Nie wysłano: {error}'
    if not rule.enabled:
        return messages.WARNING, format_html('Wysłano na kanał „{}”{}: {} Uwaga: to powiadomienie jest {}.',
                                             channel, note, content, DISABLED_NOTE)
    return messages.SUCCESS, format_html('Wysłano na kanał „{}”{}: {}', channel, note, content)


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


# Rangi Discorda: mapowanie, podgląd i synchronizacja, dopasowanie osób, historia, link zapraszający

class DiscordRoleForm(forms.ModelForm):
    class Meta:
        model = DiscordRole
        fields = ('name', 'role_id', 'cms_roles', 'sync_mode')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields['sync_mode']
        if self.instance.sync_mode != DiscordRole.SyncMode.FULL:
            # Pełną synchronizację włącza tylko „Zatwierdź i włącz” po obejrzeniu podglądu.
            field.choices = [choice for choice in field.choices if choice[0] != DiscordRole.SyncMode.FULL]
            field.help_text = ('Pełną synchronizację (z odbieraniem rangi) włączysz przyciskiem „Zatwierdź i włącz” '
                               'na stronie podglądu – lista „Role Discorda” → „Synchronizacja rang”.')
        else:
            field.help_text = ('Zmiana ID rangi albo ról CMS wymaga ponownego zatwierdzenia podglądu – do tego czasu '
                               'ranga jest tylko nadawana.')

    def clean(self):
        cleaned = super().clean()
        mode, role_id = cleaned.get('sync_mode'), cleaned.get('role_id')
        if mode and mode != DiscordRole.SyncMode.OFF and role_id:
            other = (DiscordRole.objects.filter(role_id=role_id).exclude(sync_mode=DiscordRole.SyncMode.OFF)
                     .exclude(pk=self.instance.pk).first())
            if other:
                self.add_error('sync_mode', f'Tę rangę (ID {role_id}) nadaje już {other} – wyłącz nadawanie '
                                            'w jednej z nich.')
        return cleaned


class DiscordRoleAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    form = DiscordRoleForm
    list_display = ('__str__', 'role_id', 'cms_role_names', 'mode_display')
    list_filter = ('sync_mode',)
    search_fields = ('name',)
    filter_horizontal = ('cms_roles',)
    fields = ('name', 'role_id', 'cms_roles', 'sync_mode', 'approval')
    readonly_fields = ('approval',)
    change_list_template = 'admin/core/discordrole/change_list.html'

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('cms_roles')

    @admin.display(description='Role w CMS-ie')
    def cms_role_names(self, obj):
        return ', '.join(role.name for role in obj.cms_roles.all()) or '–'

    @admin.display(description='Nadawanie przez CMS')
    def mode_display(self, obj):
        if obj.sync_mode == DiscordRole.SyncMode.FULL and not obj.full_sync_approved:
            return format_html('{} <div class="cms-error-text">Mapowanie zmieniono po zatwierdzeniu – działa tylko '
                               'nadawanie, zatwierdź podgląd ponownie.</div>', obj.get_sync_mode_display())
        return obj.get_sync_mode_display()

    @admin.display(description='Zatwierdzenie pełnej synchronizacji')
    def approval(self, obj):
        if not obj or not obj.pk:
            return 'Najpierw zapisz rangę, potem obejrzyj podgląd w „Synchronizacja rang”.'
        link = reverse('admin:core_discordrole_approve', args=[obj.pk])
        if obj.full_sync_approved and obj.full_sync_approved_at:
            who = obj.full_sync_approved_by or '–'
            return format_html('Zatwierdzono {} ({}). <a href="{}">Podgląd pełnej synchronizacji</a>',
                               timezone.localtime(obj.full_sync_approved_at).strftime('%d.%m.%Y %H:%M'), who, link)
        return format_html('Nie zatwierdzono. <a href="{}">Podgląd i zatwierdzenie pełnej synchronizacji</a>', link)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        role = form.instance
        role = DiscordRole.objects.prefetch_related('cms_roles').get(pk=role.pk)
        if role.sync_mode == DiscordRole.SyncMode.FULL and not role.full_sync_approved:
            self.message_user(request, f'{role}: zmieniono mapowanie rangi w pełnej synchronizacji – do ponownego '
                                       'zatwierdzenia podglądu CMS rangę tylko nadaje.', messages.WARNING)
        elif role.sync_mode != DiscordRole.SyncMode.OFF:
            self.message_user(request, 'Mapowanie zadziała przy najbliższej zmianie osób, synchronizacji z panelu '
                                       '(„Synchronizacja rang”) albo zadaniu sync_discord_roles.', messages.INFO)

    def get_urls(self):
        view = self.admin_site.admin_view
        return [path('synchronizacja/', view(self.sync_view), name='core_discordrole_sync'),
                path('<int:role_pk>/zatwierdz/', view(self.approve_view), name='core_discordrole_approve'),
                path('dopasuj/', view(self.match_view), name='core_discordrole_match'),
                *super().get_urls()]

    def _context(self, request, title, **context):
        from people import discord_api
        return {**self.admin_site.each_context(request), 'opts': self.model._meta, 'title': title,
                'configured': discord_api.is_configured(), 'not_configured': discord_api.NOT_CONFIGURED, **context}

    @staticmethod
    def _token(digest):
        from django.core import signing
        return signing.dumps(digest, salt='people.discord_sync')

    @staticmethod
    def _confirmed(request):
        from django.conf import settings
        from django.core import signing
        try:
            return signing.loads(request.POST.get('token', ''), salt='people.discord_sync',
                                 max_age=settings.SESSION_COOKIE_AGE)
        except signing.BadSignature:
            return ''

    def _report(self, request, outcome):
        from people.discord_sync import describe
        if outcome.error:
            self.message_user(request, outcome.error, messages.ERROR)
            return
        if outcome.stale:
            self.message_user(request, 'Stan na Discordzie albo w CMS-ie zmienił się od podglądu – sprawdź nowy podgląd '
                                       'i potwierdź ponownie.', messages.WARNING)
            return
        for change in outcome.changes:
            level = messages.ERROR if change.result == DiscordRoleChange.Result.ERROR else messages.SUCCESS
            self.message_user(request, describe(change), level)
        if not outcome.changes:
            self.message_user(request, 'Rangi są zgodne z CMS-em – nic nie zmieniono.', messages.SUCCESS)

    def sync_view(self, request):
        """Przegląd: podgląd wszystkich zarządzanych rang i „Synchronizuj teraz” (wykonuje dokładnie pokazany plan)."""
        from people.discord_sync import UNRECOGNIZED_RULE, managed_roles, plans_digest, preview, synchronize
        if not self.has_superuser_access(request):
            return redirect('admin:index')
        if request.method == 'POST':
            outcome = synchronize(source=DiscordRoleChange.Source.PANEL, actor=request.user,
                                  expected=self._confirmed(request))
            self._report(request, outcome)
            if not outcome.stale:
                return redirect('admin:core_discordrole_sync')
        roles = managed_roles()
        plans, error = preview(roles) if roles else ([], '')
        errors = DiscordRoleChange.objects.filter(result=DiscordRoleChange.Result.ERROR)
        off = DiscordRole.objects.filter(sync_mode=DiscordRole.SyncMode.OFF).order_by('name')
        return TemplateResponse(request, 'admin/core/discordrole/sync.html', self._context(
            request, 'Synchronizacja rang Discorda', plans=plans, error=error, roles=roles, off_roles=off,
            token=self._token(plans_digest(plans)), unrecognized_rule=UNRECOGNIZED_RULE,
            pending_errors=errors.count(), recent=DiscordRoleChange.objects.select_related('actor')[:10]))

    def approve_view(self, request, role_pk):
        """Podgląd rangi z odbieraniem i „Zatwierdź i włącz pełną synchronizację” (zapis zatwierdzenia i wykonanie)."""
        from django.shortcuts import get_object_or_404
        from people.discord_sync import UNRECOGNIZED_RULE, approval_preview, approve_full_sync, plans_digest
        if not self.has_superuser_access(request):
            return redirect('admin:index')
        role = get_object_or_404(DiscordRole, pk=role_pk)
        if request.method == 'POST':
            outcome = approve_full_sync(role, request.user, expected=self._confirmed(request))
            self._report(request, outcome)
            if not (outcome.error or outcome.stale):
                self.message_user(request, f'{role}: włączono pełną synchronizację.', messages.SUCCESS)
                return redirect('admin:core_discordrole_sync')
        plans, error = approval_preview(role)
        return TemplateResponse(request, 'admin/core/discordrole/approve.html', self._context(
            request, f'Pełna synchronizacja rangi {role}', role=role, plans=plans, error=error,
            token=self._token(plans_digest(plans)), unrecognized_rule=UNRECOGNIZED_RULE,
            no_roles=not role.cms_roles.exists()))

    def match_view(self, request):
        """„Dopasuj po imieniu i nazwisku”: propozycje z listy członków serwera; zapis tylko zaznaczonych."""
        from people import discord_api, discord_matching
        from people.discord_sync import admin_messages, report_to
        if not self.has_superuser_access(request):
            return redirect('admin:index')
        matching, error = None, ''
        if discord_api.is_configured():
            try:
                matching = discord_matching.match(discord_api.DiscordClient().members())
            except discord_api.DiscordError as exception:
                error = str(exception)
        if request.method == 'POST' and matching is not None:
            chosen = {}
            for name, value in request.POST.items():
                if name.startswith('link_') and name[5:].isdigit() and value:
                    chosen[int(name[5:])] = value
            replace = {int(value) for value in request.POST.getlist('replace') if value.isdigit()}
            from django.db import transaction
            with transaction.atomic():
                report_to(request, admin_messages(self, request))
                saved, errors = discord_matching.apply(matching, chosen, replace)
            for text in errors:
                self.message_user(request, text, messages.ERROR)
            if saved:
                self.message_user(request, 'Zapisano identyfikator Discorda: ' + ', '.join(map(str, saved)) + '.',
                                  messages.SUCCESS)
            elif not errors:
                self.message_user(request, 'Nie zaznaczono żadnej propozycji.', messages.WARNING)
            return redirect('admin:core_discordrole_match')
        return TemplateResponse(request, 'admin/core/discordrole/match.html', self._context(
            request, 'Dopasuj osoby do kont Discorda', matching=matching, error=error))


admin.site.register(DiscordRole, DiscordRoleAdmin)


@admin.register(DiscordRoleChange)
class DiscordRoleChangeAdmin(ReadOnlyAdminMixin, SuperuserOnlyAdminMixin, admin.ModelAdmin):
    """Historia nadań i odebrań rang (tylko do odczytu)."""
    list_display = ('created_at', 'person_display', 'role_label', 'action', 'result', 'reason', 'source', 'actor',
                    'short_response')
    # Filtr dat zamiast date_hierarchy: hierarchia po DateTimeField wymaga w MySQL tabel stref czasowych.
    list_filter = ('created_at', 'result', 'action', 'source', 'discord_role')
    search_fields = ('person_label', 'discord_user_id', 'role_label', 'reason')
    list_select_related = ('person', 'actor')
    readonly_fields = ('created_at', 'person', 'person_label', 'discord_user_id', 'discord_role', 'role_label',
                       'role_id', 'action', 'result', 'reason', 'source', 'actor', 'response')
    fields = readonly_fields

    @admin.display(description='osoba', ordering='person_label')
    def person_display(self, obj):
        if obj.person_id:
            return format_html('<a href="{}">{}</a>', reverse('admin:people_person_change', args=[obj.person_id]),
                               obj.person_label or obj.person)
        return obj.person_label or f'konto {obj.discord_user_id} (bez osoby)'

    @admin.display(description='odpowiedź')
    def short_response(self, obj):
        return obj.response[:120] or '–'


@admin.register(DiscordSettings)
class DiscordSettingsAdmin(SingleRecordAdminMixin, SuperuserOnlyAdminMixin, admin.ModelAdmin):
    fields = ('invite_url',)
