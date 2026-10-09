"""Ostrzega, gdy zainstalowany Django nie odpowiada wersji z requirements.txt."""
import re

import django
from django.conf import settings
from django.core.checks import Warning, register


def pinned_django_version():
    """Czyta przypiętą wersję z requirements.txt, aby nie dublować jej w kodzie."""
    try:
        text = (settings.BASE_DIR / "requirements.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^django==([0-9][0-9a-z.]*)\s*$", text, re.IGNORECASE | re.MULTILINE)
    return match[1] if match else None


@register()
def runtime_version(app_configs, **kwargs):
    pinned = pinned_django_version()
    if pinned and django.get_version() != pinned:
        return [Warning(
            f"Wersja Django ({django.get_version()}) różni się od przypiętej {pinned}.",
            hint="Zainstaluj requirements.txt w środowisku używanym przez aplikację.",
            id="core.W008",
        )]
    return []
