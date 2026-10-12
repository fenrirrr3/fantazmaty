from django.core.management.base import BaseCommand

from core.discord_notifications import send_waiting_reminders


class Command(BaseCommand):
    help = 'Przypomnienia Discord o tekstach, które od 7, 14 lub 21 dni czekają na osobę (uruchamiać raz dziennie).'

    def handle(self, *args, **options):
        self.stdout.write(f'Zakolejkowane przypomnienia: {send_waiting_reminders()}')
