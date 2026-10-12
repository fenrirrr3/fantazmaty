from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    verbose_name = "Publikacje i organizacja"

    def ready(self):
        from core.search_lookup import register
        register()
        from core import runtime_checks  # noqa: F401
        from core.workflow_events import install
        install()
        from core.discord_notifications import install as install_discord_notifications
        install_discord_notifications()
        from core.edit_versions import install as install_edit_versions
        install_edit_versions()
