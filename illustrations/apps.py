from django.apps import AppConfig


class IllustrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "illustrations"
    verbose_name = "Ilustracje"

    def ready(self):
        from . import signals  # noqa: F401