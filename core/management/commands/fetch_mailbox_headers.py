from django.core.management.base import BaseCommand, CommandError

from core.services.mailbox_snapshots import refresh_snapshots


class Command(BaseCommand):
    help = 'Pobiera same nagłówki wiadomości ze skrzynek zgłoszeń i rekrutacji (zadanie nocne PythonAnywhere).'

    def handle(self, *args, **options):
        summary = refresh_snapshots()
        for item in summary:
            if item['status'] == 'ok':
                self.stdout.write(f"{item['label']}: pobrano nagłówki – {item['count']}.")
            elif item['status'] == 'skipped':
                self.stdout.write(f"{item['label']}: pominięto – {item['message']}")
            else:
                self.stderr.write(f"{item['label']}: błąd – {item['message']}")
        statuses = {item['status'] for item in summary}
        if 'error' in statuses and 'ok' not in statuses:
            raise CommandError('Nie udało się pobrać nagłówków z żadnej skrzynki.')
