from django.apps import AppConfig


class PeopleConfig(AppConfig):
    name = 'people'
    verbose_name = "Ludzie"

    def ready(self):
        from . import dropbox_access, identity, signals  # noqa: F401
