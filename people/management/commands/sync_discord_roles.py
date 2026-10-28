"""Wyrównuje rangi Discorda z rolami w CMS-ie (zadanie zaplanowane na serwerze albo ręcznie)."""
from django.core.management.base import BaseCommand, CommandError

from core.notification_models import DiscordRoleChange
from people import discord_api
from people.discord_sync import NO_MANAGED_ROLES, UNRECOGNIZED_RULE, describe, managed_roles, preview, synchronize


class Command(BaseCommand):
    help = ("Nadaje brakujące rangi Discorda z mapowania („Role Discorda”) i – przy pełnej synchronizacji – odbiera "
            "nadmiarowe; ponawia wcześniejsze błędy. Innych rang i nierozpoznanych członków serwera nie rusza. "
            "--dry-run wypisuje plan bez zmian.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Pokaż plan bez zmian na Discordzie i w historii.")

    def handle(self, *args, dry_run=False, **options):
        if not discord_api.is_configured():
            raise CommandError(discord_api.NOT_CONFIGURED)
        roles = managed_roles()
        if not roles:
            self.stdout.write(NO_MANAGED_ROLES)
            return
        if dry_run:
            return self.dry_run(roles)
        outcome = synchronize(source=DiscordRoleChange.Source.COMMAND)
        if outcome.error:
            raise CommandError(outcome.error)
        for change in outcome.changes:
            write = self.stderr.write if change.result == DiscordRoleChange.Result.ERROR else self.stdout.write
            write(describe(change))
        errors = len(outcome.errors)
        summary = f"Rang: {len(outcome.plans)}, zmian: {len(outcome.changes) - errors}, błędów: {errors}."
        self.stdout.write(self.style.WARNING(summary) if errors else self.style.SUCCESS(summary))

    def dry_run(self, roles):
        plans, error = preview(roles)
        if error:
            raise CommandError(error)
        for plan in plans:
            self.stdout.write(f"[próba] {plan.role} ({plan.mode_label}): mają {plan.has}, do nadania "
                              f"{len(plan.grant)}, do odebrania {len(plan.revoke) if plan.applies_revoke else 0}.")
            for entry in plan.grant:
                self.stdout.write(f"[próba]   nadanie: {entry.label} ({entry.user_id})")
            for entry in plan.revoke:
                verb = "odebranie" if plan.applies_revoke else "straciłby przy pełnej synchronizacji"
                self.stdout.write(f"[próba]   {verb}: {entry.label} ({entry.user_id})")
            if plan.unrecognized:
                self.stdout.write(f"[próba]   nierozpoznani z rangą: {len(plan.unrecognized)} – {UNRECOGNIZED_RULE}")
