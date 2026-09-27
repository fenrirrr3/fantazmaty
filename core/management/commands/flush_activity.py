import json
import os
import re
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from core.activity_spool import spool_directory
from core.models import UserActivity


def validated_record(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    expected = {'created_at','source_key','user_id','actor','method','action','target','path','status_code'}
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError('Nieprawidłowy format wpisu.')
    stamp = parse_datetime(data.pop('created_at'))
    key = data.pop('source_key')
    if stamp is None or timezone.is_naive(stamp) or key != path.stem or not re.fullmatch(r'[0-9a-f]{32}', key):
        raise ValueError('Niepoprawne metadane wpisu.')
    if data['user_id'] is not None and (type(data['user_id']) is not int or data['user_id'] <= 0):
        raise ValueError('Nieprawidłowe konto.')
    for field, limit in {'actor':254,'method':10,'action':255,'target':255,'path':1000}.items():
        if not isinstance(data[field], str) or len(data[field]) > limit:
            raise ValueError('Nieprawidłowe pole tekstowe.')
    if type(data['status_code']) is not int or not 100 <= data['status_code'] <= 599:
        raise ValueError('Nieprawidłowy status HTTP.')
    return data, stamp, key


class Command(BaseCommand):
    help = 'Przenosi lokalny dziennik do bazy; wadliwe wpisy izoluje bez blokowania kolejki.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=10000)

    def handle(self, *args, **options):
        if options['limit'] < 1:
            raise CommandError('Limit musi być dodatni.')
        count = quarantined = failed = 0
        directory = spool_directory()
        for path in sorted(directory.glob('*.json'))[:options['limit']]:
            try:
                data, stamp, key = validated_record(path)
            except FileNotFoundError:
                continue
            except (ValueError, TypeError, KeyError, UnicodeError):
                quarantine = directory / 'quarantine'
                quarantine.mkdir(mode=0o700, exist_ok=True)
                try:
                    os.replace(path, quarantine / path.name)
                except FileNotFoundError:
                    continue
                quarantined += 1
                self.stderr.write('Odłożono uszkodzony wpis: ' + path.name)
                continue
            except OSError:
                failed += 1
                self.stderr.write('Nie można odczytać wpisu: ' + path.name)
                continue
            try:
                if not get_user_model().objects.filter(pk=data['user_id']).exists():
                    data['user_id'] = None
                with transaction.atomic():
                    entry, created = UserActivity.objects.get_or_create(source_key=key, defaults=data)
                    if created:
                        UserActivity.objects.filter(pk=entry.pk).update(created_at=stamp)
                path.unlink(missing_ok=True)
                count += 1
            except Exception:
                # Transient database errors must not quarantine valid records.
                failed += 1
                self.stderr.write('Nie przeniesiono wpisu; pozostaje w kolejce: ' + path.name)
        self.stdout.write(f'Przeniesiono wpisów: {count}. Odłożono uszkodzonych: {quarantined}. Do ponowienia: {failed}.')
        if failed:
            raise CommandError('Nie wszystkie poprawne wpisy udało się przenieść. Ponów synchronizację.')
