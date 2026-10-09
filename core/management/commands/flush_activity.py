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


BATCH_SIZE = 500


class Command(BaseCommand):
    help = 'Przenosi lokalny dziennik do bazy; wadliwe wpisy izoluje bez blokowania kolejki.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=10000)

    def handle(self, *args, **options):
        if options['limit'] < 1:
            raise CommandError('Limit musi być dodatni.')
        self.count = self.quarantined = self.failed = 0
        directory = spool_directory()
        if not directory.is_dir():
            self.stdout.write('Przeniesiono wpisów: 0. Odłożono uszkodzonych: 0. Do ponowienia: 0.')
            return
        # Oldest names first is irrelevant (keys are random); created_at is stored in each entry.
        paths = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.endswith('.json') and not entry.name.startswith('.'):
                    paths.append(directory / entry.name)
                    if len(paths) >= options['limit']:
                        break
        for start in range(0, len(paths), BATCH_SIZE):
            self.flush_batch(directory, paths[start:start + BATCH_SIZE])
        self.stdout.write(f'Przeniesiono wpisów: {self.count}. Odłożono uszkodzonych: {self.quarantined}. Do ponowienia: {self.failed}.')
        if self.failed:
            raise CommandError('Nie wszystkie poprawne wpisy udało się przenieść. Ponów synchronizację.')

    def quarantine(self, directory, path):
        quarantine = directory / 'quarantine'
        quarantine.mkdir(mode=0o700, exist_ok=True)
        try:
            os.replace(path, quarantine / path.name)
        except FileNotFoundError:
            return
        self.quarantined += 1
        self.stderr.write('Odłożono uszkodzony wpis: ' + path.name)

    def flush_batch(self, directory, paths):
        records = []
        for path in paths:
            try:
                records.append((path, *validated_record(path)))
            except FileNotFoundError:
                continue
            except (ValueError, TypeError, KeyError, UnicodeError):
                self.quarantine(directory, path)
            except OSError:
                self.failed += 1
                self.stderr.write('Nie można odczytać wpisu: ' + path.name)
        if not records:
            return
        try:
            self.write_batch(records)
        except Exception:
            # Transient database errors must not quarantine valid records;
            # retry one by one so a single conflicting entry cannot block the rest.
            for record in records:
                try:
                    self.write_batch([record])
                except Exception:
                    self.failed += 1
                    self.stderr.write('Nie przeniesiono wpisu; pozostaje w kolejce: ' + record[0].name)

    def write_batch(self, records):
        user_ids = {data['user_id'] for _, data, _, _ in records if data['user_id'] is not None}
        known_users = set(get_user_model().objects.filter(pk__in=user_ids).values_list('pk', flat=True))
        with transaction.atomic():
            keys = [key for _, _, _, key in records]
            existing = set(UserActivity.objects.filter(source_key__in=keys).values_list('source_key', flat=True))
            fresh = [(data, stamp, key) for _, data, stamp, key in records if key not in existing]
            UserActivity.objects.bulk_create([
                UserActivity(source_key=key, **{**data, 'user_id': data['user_id'] if data['user_id'] in known_users else None})
                for data, stamp, key in fresh
            ])
            # created_at uses auto_now_add, so the original request time is restored afterwards.
            stamps = {key: stamp for _, stamp, key in fresh}
            saved = list(UserActivity.objects.filter(source_key__in=stamps).only('pk', 'source_key', 'created_at'))
            for entry in saved:
                entry.created_at = stamps[entry.source_key]
            UserActivity.objects.bulk_update(saved, ['created_at'])
        for path, _, _, _ in records:
            path.unlink(missing_ok=True)
        self.count += len(records)
