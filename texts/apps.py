from django.apps import AppConfig


class TextsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "texts"
    verbose_name = "Teksty"

    def ready(self):
        from . import signals  # noqa: F401