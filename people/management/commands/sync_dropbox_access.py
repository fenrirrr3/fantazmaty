"""Uzgadnia dostęp do folderów Dropbox z CMS-em (ręcznie albo jako zadanie okresowe na serwerze)."""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from people.dropbox_access import sync
from people.dropbox_sharing import NOT_CONFIGURED, is_configured
from people.models import DropboxAccess


class Command(BaseCommand):
    help = ("Nadaje, zmienia i odbiera dostęp do folderów Dropbox według CMS-u (wiersze czekające, z błędem "
            "i nadane – te ostatnie są tylko sprawdzane). --dry-run pokazuje plan bez zmian.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Pokaż, co zostałoby zmienione, bez zmian w Dropboxie i bazie.")

    def handle(self, *args, dry_run=False, **options):
        if not dry_run and not is_configured():
            raise CommandError(NOT_CONFIGURED)
        rows = DropboxAccess.objects.filter(Q(wanted=True) | ~Q(granted_email="") | ~Q(state=DropboxAccess.State.REMOVED))
        if dry_run and not is_configured():
            self.stdout.write("Dropbox nie jest skonfigurowany – plan bez sprawdzenia stanu folderów w Dropboxie.")
        results = sync(rows, dry_run=dry_run)
        prefix = "[próba] " if dry_run else ""
        errors = 0
        for result in results:
            if result.error:
                errors += 1
                self.stderr.write(prefix + result.describe())
            elif result.action or result.removed:
                self.stdout.write(prefix + result.describe())
        changed = sum(1 for result in results if (result.action or result.removed) and not result.error)
        summary = f"{prefix}Wierszy: {len(results)}, zmian: {changed}, błędów: {errors}."
        self.stdout.write(self.style.WARNING(summary) if errors else self.style.SUCCESS(summary))
