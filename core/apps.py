from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    verbose_name = "Publikacje i organizacja"

    def ready(self):
        from core.search_lookup import register
        register()
        from core.admin_access import install as install_admin_access
        install_admin_access()
        from core import runtime_checks  # noqa: F401
        from core.workflow_events import install
        install()
