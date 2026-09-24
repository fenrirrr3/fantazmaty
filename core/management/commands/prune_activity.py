from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from core.models import UserActivity

class Command(BaseCommand):
    help = 'Usuwa stare udane odwiedziny; zachowuje operacje i historię workflow.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=180)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        if options['days'] < 1:
            raise CommandError('Liczba dni musi być dodatnia.')
        records = UserActivity.objects.filter(created_at__lt=timezone.now()-timedelta(days=options['days']),
            method__in=['GET', 'HEAD'], status_code__lt=400).exclude(action__in=['Logowanie', 'Wylogowanie'])
        # Preserve each user's last known activity for the inactivity report.
        from django.db.models import OuterRef, Subquery
        latest = UserActivity.objects.filter(user_id=OuterRef('user_id'), status_code__lt=400).order_by('-created_at', '-pk')
        last = UserActivity.objects.filter(user__isnull=False).order_by().values('user_id').distinct().annotate(last=Subquery(latest.values('pk')[:1])).values_list('last', flat=True)
        records = records.exclude(pk__in=list(last))
        count = records.count()
        if not options['dry_run']:
            while True:
                ids = list(records.values_list('pk', flat=True)[:1000])
                if not ids:
                    break
                UserActivity.objects.filter(pk__in=ids).delete()
        self.stdout.write(f"{'Do usunięcia' if options['dry_run'] else 'Usunięto'}: {count}")
