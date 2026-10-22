"""Uzgadnia dostęp do folderów Dropbox z CMS-em (ręcznie albo jako zadanie okresowe na serwerze)."""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from people.dropbox_access import sync
from people.dropbox_sharing import NOT_CONFIGURED, is_configured
from people.models import DropboxAccess, DropboxFolder


class Command(BaseCommand):
    help = ("Nadaje, zmienia i odbiera dostęp do folderów Dropbox według CMS-u (wiersze czekające, z błędem "
            "i nadane – te ostatnie są tylko sprawdzane). --dry-run pokazuje plan bez zmian. --import-members "
            "przejmuje obecnych członków folderów (tylko dopasowanie po adresie Dropbox osób z ekipy).")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Pokaż, co zostałoby zmienione, bez zmian w Dropboxie i bazie.")
        parser.add_argument("--import-members", action="store_true",
                            help="Przejmij obecnych członków folderów z Dropboxa (bez zaproszeń i zmian w Dropboxie).")

    def handle(self, *args, dry_run=False, import_members=False, **options):
        if import_members:
            return self.import_members(dry_run)
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

    def import_members(self, dry_run):
        """Przejęcie jak w adminie, ale tylko grupy bez decyzji administratora (dopasowanie po adresie Dropbox,
        poziom z Dropboxa); dopasowania po innym adresie i osoby spoza ekipy są tylko wypisywane."""
        from people import dropbox_import
        from people.dropbox_sharing import DropboxError
        if not is_configured():
            raise CommandError(NOT_CONFIGURED)
        try:
            plans = dropbox_import.plan_folders(DropboxFolder.objects.order_by("name", "pk"))
        except DropboxError as error:
            raise CommandError(str(error)) from None
        prefix = "[próba] " if dry_run else ""
        if not dry_run:
            plans, counts = dropbox_import.apply_plans(plans)
        for plan in plans:
            for entry in plan.entries:
                if entry.group not in ("managed", "outside"):
                    person = f" ({entry.person})" if entry.person else ""
                    note = f" {entry.note}" if entry.note else ""
                    self.stdout.write(f"{prefix}{plan.folder.name}: {dropbox_import.GROUP_LABELS[entry.group]} – "
                                      f"{entry.email}{person}, {entry.dropbox_level}.{note}")
            count = sum(1 for entry in plan.entries if entry.group in ("import", "invited", "level"))
            message = dropbox_import.summary(plan, count if dry_run else counts[plan.folder.pk], planned=dry_run)
            write = self.stderr.write if plan.error else self.stdout.write
            write(prefix + message)
