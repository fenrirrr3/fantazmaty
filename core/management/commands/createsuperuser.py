from django.contrib.auth.management.commands.createsuperuser import Command as BaseCommand
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from core.account_identity import validate_account_email
import os


class Command(BaseCommand):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.UserModel.REQUIRED_FIELDS = list(dict.fromkeys([
            *self.UserModel.REQUIRED_FIELDS, "first_name", "last_name",
        ]))

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
