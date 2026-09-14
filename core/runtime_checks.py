import django
from django.core.checks import Warning, register
@register()
def runtime_version(app_configs, **kwargs):
    if django.get_version() != '5.2.17':
        return [Warning('Wersja Django różni się od przypiętej 5.2.17.', hint='Zainstaluj requirements.txt w środowisku używanym przez aplikację.', id='core.W008')]
    return []
