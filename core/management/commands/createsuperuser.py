from contextlib import contextmanager
import os

from django.contrib.auth.management.commands.createsuperuser import Command as BaseCommand
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

from core.account_identity import validate_account_email

NAME_FIELDS = ("first_name", "last_name")


class Command(BaseCommand):
    @contextmanager
    def _names_required(self):
        """Imię i nazwisko są wymagane tylko na czas działania komendy; model User pozostaje bez zmian."""
        model = self.UserModel
        had_own = "REQUIRED_FIELDS" in vars(model)
        original = model.REQUIRED_FIELDS
        model.REQUIRED_FIELDS = list(dict.fromkeys([*original, *NAME_FIELDS]))
        try:
            yield
        finally:
            if had_own:
                model.REQUIRED_FIELDS = original
            else:
                del model.REQUIRED_FIELDS

    def add_arguments(self, parser):
        with self._names_required():
            super().add_arguments(parser)

    def handle(self, *args, **options):
        self.account_database = options['database']
        email = options.get('email')
        if email is None and not options['interactive']:
            email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '')
        if email is not None:
            try:
                options['email'] = validate_account_email(email, using=self.account_database)
            except ValidationError as error:
                raise CommandError(' '.join(error.messages)) from error
        with self._names_required():
            return super().handle(*args, **options)

    def get_input_data(self, field, message, default=None):
        value = super().get_input_data(field, message, default)
        if field.name == 'email' and value is not None:
            try:
                return validate_account_email(value, using=self.account_database)
            except ValidationError as error:
                self.stderr.write(' '.join(error.messages))
                return None
        return value
