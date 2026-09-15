"""Deliver the workflow outbox outside web requests; never retry uncertain sends."""
import time
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone
from core.models import WorkflowEvent
from core.workflow_events import deliver

class Command(BaseCommand):
    help = 'Wysyła oczekujące powiadomienia workflow; --watch stale obsługuje kolejkę.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true')
        parser.add_argument('--interval', type=int, default=15)
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--stale-minutes', type=int, default=10)
        parser.add_argument('--retry-failed', action='store_true', help='Jednorazowo ponawia wysyłki z potwierdzonym błędem.')
        parser.add_argument('--retry-disabled', action='store_true', help='Ponawia pominięte wysyłki po skonfigurowaniu kanału.')
        parser.add_argument('--database', default='default')

    def handle(self, *args, **options):
        if options['interval'] < 1 or not 1 <= options['limit'] <= 1000 or options['stale_minutes'] < 2:
            raise CommandError('Wymagane: interval >= 1, limit 1–1000, stale-minutes >= 2.')
        events = WorkflowEvent.objects.using(options['database'])
        statuses = []
        if options['retry_failed']: statuses.append('failed')
        if options['retry_disabled']: statuses.append('disabled')
        if statuses:
            count = events.filter(status__in=statuses).update(status='pending', sending_started_at=None)
            self.stdout.write(f'Przywrócono do kolejki: {count}')
        try:
            while True:
                close_old_connections()
                threshold = timezone.now() - timedelta(minutes=options['stale_minutes'])
                stuck = events.filter(status='sending').filter(Q(sending_started_at__lt=threshold) | Q(sending_started_at__isnull=True, created_at__lt=threshold))
                recovered = stuck.update(status='unknown')
                ids = list(events.filter(status='pending').order_by('created_at', 'pk').values_list('pk', flat=True)[:options['limit']])
                for pk in ids:
                    deliver(pk, options['database'])
                if recovered or ids or not options['watch']:
                    self.stdout.write(f'Sprawdzono oczekujące: {len(ids)}; przerwane oznaczone jako niepewne: {recovered}; pozostało oczekujących: {events.filter(status="pending").count()}')
                if not options['watch']: break
                time.sleep(options['interval'])
        except KeyboardInterrupt:
            self.stdout.write('Zatrzymano obsługę kolejki.')
