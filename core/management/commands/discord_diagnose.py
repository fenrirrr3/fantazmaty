"""Diagnostyka powiadomień Discord (tylko odczyt): stan reguł, antologie z ograniczonymi powiadomieniami,
ostatnie wysyłki i dziennik zmian workflow.

Nie wypisuje adresów webhooków ani danych osób – z historii pokazuje tylko datę, stan i tytuł tekstu.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.discord_notifications import configured_channel_names, parse_mention, rule_problems
from core.models import DiscordNotification, DiscordRole, NotificationRule, WorkflowEvent
from texts.models import Anthology

HISTORY = 10
EVENT_DAYS = 7
COMPLETED_LINE = 'Zakończono etap:'


def yes_no(value):
    return 'tak' if value else 'nie'


class Command(BaseCommand):
    help = ('Pokazuje stan reguł powiadomień Discord, ostatnie wpisy historii wysyłek i liczbę zmian workflow '
            'z ostatnich 7 dni. Niczego nie zmienia.')

    def handle(self, *args, **options):
        out = self.stdout.write
        configured = configured_channel_names()
        roles = {r.name.lower(): r.role_id for r in DiscordRole.objects.all()}
        if configured is None:
            out('Kanały na serwerze (DISCORD_WEBHOOKS): BŁĘDNA KONFIGURACJA – żadna wiadomość nie wyjdzie.')
        else:
            out(f'Kanały na serwerze (DISCORD_WEBHOOKS): {", ".join(sorted(configured)) or "brak"}')
        out('')
        out('Reguły:')
        for rule in NotificationRule.objects.prefetch_related('templates'):
            if not rule.channel:
                channel = 'brak'
            elif configured is None:
                channel = f'„{rule.channel}”'
            else:
                channel = f'„{rule.channel}” (w konfiguracji serwera: {yes_no(rule.channel in configured)})'
            try:
                parse_mention(rule.mention, roles)
                mention = f'{rule.mention} (poprawne)' if rule.mention else 'brak'
            except ValueError:
                mention = f'{rule.mention} (BŁĘDNE – nie ma takiej roli)'
            out(f'- {rule} [{rule.trigger}]: włączona: {yes_no(rule.enabled)}; kanał: {channel}; '
                f'treści: {len(rule.templates.all())}; oznaczenie: {mention}')
            problems = rule_problems(rule, configured, roles)
            out(f'  Uwagi: {"; ".join(problems)}' if problems else '  Działa.')
        out('')
        limited = Anthology.objects.exclude(discord_notifications=Anthology.DiscordNotifications.ALL).order_by('title', 'pk')
        out('Antologie z ograniczonymi powiadomieniami:' + ('' if limited else ' brak'))
        for anthology in limited:
            out(f'- „{anthology.title}”: {anthology.get_discord_notifications_display()}')
        out('')
        out(f'Historia wysyłek (ostatnie {HISTORY} wpisów na zdarzenie):')
        for trigger, label in NotificationRule.Trigger.choices:
            items = list(DiscordNotification.objects.filter(trigger=trigger).select_related('text')[:HISTORY])
            out(f'- {label} [{trigger}]: {"brak wpisów" if not items else ""}'.rstrip())
            for item in items:
                if item.text_id:
                    subject = f'„{item.text.title}”'
                elif item.review_id:
                    subject = 'zgłoszenie do recenzji'
                else:
                    subject = 'test z panelu'
                when = timezone.localtime(item.created_at).strftime('%Y-%m-%d %H:%M')
                out(f'  {when} – {item.get_status_display()} – {subject}')
        out('')
        since = timezone.now() - timedelta(days=EVENT_DAYS)
        events = WorkflowEvent.objects.filter(kind=WorkflowEvent.Kind.WORKFLOW, created_at__gte=since)
        completions = events.filter(details__contains=COMPLETED_LINE).count()
        rule = NotificationRule.Trigger.STAGE_COMPLETED
        sent = DiscordNotification.objects.filter(trigger=rule, created_at__gte=since).count()
        out(f'Dziennik zmian workflow – ostatnie {EVENT_DAYS} dni: {events.count()} zmian, w tym {completions} '
            f'z zakończeniem etapu; wpisy „{rule.label}” w historii wysyłek: {sent}.')
        if completions and not sent:
            out(f'Etapy się kończą, ale nie powstała żadna wiadomość „{rule.label}” – sprawdź uwagi tej reguły '
                'powyżej (najczęściej: reguła wyłączona albo bez kanału lub treści) i ustawienia antologii.')
