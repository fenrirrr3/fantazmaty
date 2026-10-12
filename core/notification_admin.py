"""Panel admina: reguły powiadomień Discord, role, historia wysyłek i testowe wysyłanie."""
import random

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from authors.admin import SuperuserOnlyAdminMixin
from core.discord_notifications import mention_text, parse_mention, render
from core.models import DiscordNotification, DiscordRole, NotificationRule, NotificationTemplate
from core.notification_models import TEMPLATE_TAGS

TAGS_HELP = 'Tagi: ' + ', '.join(f'[{tag}] – {label}' for tag, label in TEMPLATE_TAGS.items()) + '.'


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
        self.fields['channel'].choices = [('', '– wybierz kanał –')] + [(n, n) for n in names]
        self.fields['channel'].help_text = ('Kanały z konfiguracji webhooków serwera (DISCORD_WEBHOOKS).'
                                            if configured_channels() else
                                            'Na serwerze nie skonfigurowano żadnego webhooka (DISCORD_WEBHOOKS).')

    def clean_mention(self):
        value = self.cleaned_data.get('mention', '').strip()
        try:
            parse_mention(value)
        except ValueError as exc:
            raise forms.ValidationError(str(exc))
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('enabled') and not cleaned.get('channel'):
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
    list_display = ('__str__', 'enabled', 'channel', 'mention', 'template_count')
    list_display_links = ('__str__',)
    list_editable = ()
    fields = ('trigger_label', 'enabled', 'channel', 'mention')
    readonly_fields = ('trigger_label',)
    change_list_template = 'admin/core/notificationrule/change_list.html'

    @admin.display(description='Kiedy')
    def trigger_label(self, obj):
        return obj.get_trigger_display()

    @admin.display(description='Treści')
    def template_count(self, obj):
        return obj.templates.count()

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
            trigger = request.POST.get('trigger', '')
            rule = rules.get(trigger)
            if rule is None:
                messages.error(request, 'Nieznany rodzaj wiadomości.')
            else:
                level, text = send_test(rule, form.cleaned_data)
                messages.add_message(request, level, text)
            return TemplateResponse(request, 'admin/core/notificationrule/test.html', self.test_context(request, form, rules))
        return TemplateResponse(request, 'admin/core/notificationrule/test.html', self.test_context(request, form, rules))

    def test_context(self, request, form, rules):
        buttons = [{'trigger': value, 'label': label, 'rule': rules.get(value),
                    'url': reverse('admin:core_notificationrule_change', args=[rules[value].pk]) if value in rules else ''}
                   for value, label in NotificationRule.Trigger.choices]
        return {**self.admin_site.each_context(request), 'title': 'Testowe wysyłanie powiadomień Discord',
                'form': form, 'buttons': buttons, 'opts': self.model._meta, 'tags_help': TAGS_HELP}


class TestNotificationForm(forms.Form):
    title = forms.CharField(label='[tytuł]', max_length=300, initial='Latarnik')
    anthology = forms.CharField(label='[antologia]', max_length=300, initial='Cienie miasta', required=False)
    genre = forms.CharField(label='[gatunek]', max_length=100, initial='fantasy', required=False)
    length = forms.IntegerField(label='[długość]', min_value=0, max_value=10_000_000, initial=25000, required=False)
    stage = forms.CharField(label='[etap] (dla „Zakończenie etapu”)', max_length=100, initial='Redakcja', required=False,
                            help_text='Przy pozostałych wiadomościach [etap] przyjmuje nazwę etapu z przycisku.')
    mark_test = forms.BooleanField(label='Dopisz „[TEST]” na początku wiadomości', initial=True, required=False)


STAGE_NAMES = {
    'review_new': 'Recenzja', 'ready_for_editing': 'Do redakcji', 'first_proofreading': 'Pierwsza korekta',
    'third_proofreading': 'Trzecia korekta', 'first_verification': 'Pierwsza weryfikacja',
    'second_verification': 'Druga weryfikacja', 'third_verification': 'Trzecia weryfikacja',
}


def send_test(rule, data):
    """Wysyła od razu (bez kolejki) wiadomość z reguły – także wyłączonej – i zapisuje ją w historii."""
    from core.discord_webhook import channels, send_message, ConfigurationError, DeliveryError
    templates = [t.content for t in rule.templates.all()]
    if not rule.channel:
        return messages.ERROR, f'„{rule}”: wybierz najpierw kanał w ustawieniach tej wiadomości.'
    if not templates:
        return messages.ERROR, f'„{rule}”: dodaj najpierw co najmniej jedną treść wiadomości.'
    try:
        role_ids, here = parse_mention(rule.mention)
    except ValueError as exc:
        return messages.ERROR, f'„{rule}”: {exc}'
    length = data.get('length')
    context = {'tytuł': data['title'], 'antologia': data.get('anthology', ''), 'gatunek': data.get('genre', ''),
               'długość': f'{length:,}'.replace(',', ' ') if length is not None else '',
               'etap': (data.get('stage') or 'Redakcja') if rule.trigger == 'stage_completed' else STAGE_NAMES.get(rule.trigger, '')}
    content = render(random.choice(templates), context, mention_text(role_ids, here))
    if data.get('mark_test'):
        content = ('[TEST] ' + content)[:2000]
    item = DiscordNotification.objects.create(trigger=rule.trigger, channel=rule.channel, content=content,
                                              role_ids=','.join(role_ids), mention_here=here, status='sending')
    try:
        configured = channels()
        if rule.channel not in configured:
            item.status = 'disabled'
            item.save(update_fields=['status'])
            return messages.ERROR, f'Kanału „{rule.channel}” nie ma w konfiguracji webhooków serwera.'
        item.message_id = send_message(configured[rule.channel], content, role_ids=role_ids, here=here)
        item.status = 'sent'
        item.save(update_fields=['status', 'message_id'])
        return messages.SUCCESS, format_html('Wysłano na kanał „{}”: {}', rule.channel, content)
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
