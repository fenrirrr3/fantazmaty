from django.contrib.auth.management.commands.createsuperuser import Command as BaseCommand


class Command(BaseCommand):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.UserModel.REQUIRED_FIELDS = list(dict.fromkeys([
            *self.UserModel.REQUIRED_FIELDS, "first_name", "last_name",
        ]))
