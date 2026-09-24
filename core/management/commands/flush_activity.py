import json
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime
from core.activity_spool import spool_directory
from core.models import UserActivity

class Command(BaseCommand):
    help = 'Przenosi lokalny dziennik aktywności do bazy, bez duplikowania wpisów.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=10000)

    def handle(self, *args, **options):
        if options['limit'] < 1:
            raise CommandError('Limit musi być dodatni.')
        count = 0
        for path in sorted(spool_directory().glob('*.json'))[:options['limit']]:
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                continue
            try:
                stamp = parse_datetime(data.pop('created_at'))
                key = data.pop('source_key')
                if stamp is None or key != path.stem:
                    raise ValueError('Niepoprawne metadane wpisu.')
                if not get_user_model().objects.filter(pk=data['user_id']).exists():
                    data['user_id'] = None
                with transaction.atomic():
                    entry, created = UserActivity.objects.get_or_create(source_key=key, defaults=data)
                    if created:
                        UserActivity.objects.filter(pk=entry.pk).update(created_at=stamp)
                path.unlink(missing_ok=True)
                count += 1
            except Exception as exc:
                raise CommandError('Nie udało się przenieść wpisu ' + path.name + '; pozostaje w kolejce.') from exc
        self.stdout.write(f'Przeniesiono wpisów: {count}')
