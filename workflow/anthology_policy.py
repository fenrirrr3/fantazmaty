from django.core.exceptions import ValidationError
from texts.models import Anthology

def require_working_anthology(text):
    """Call inside a write transaction, after locking Text."""
    if not text.anthology_id:
        return
    anthology = Anthology.objects.using(text._state.db or 'default').select_for_update().get(pk=text.anthology_id)
    if anthology.status == Anthology.Status.READY:
        raise ValidationError('Antologia jest gotowa. Aby zmienić workflow, najpierw ustaw ją na „W przygotowaniu”.')
