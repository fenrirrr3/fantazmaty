from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    verbose_name = "Publikacje i organizacja"

    def ready(self):
        from core.search_lookup import register
        register()
