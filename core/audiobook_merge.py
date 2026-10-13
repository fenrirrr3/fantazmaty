"""Scalanie wpisów spisu lektorów i montażystów (akcja w panelu admina i komenda merge_audio_contributor)."""
from django.db import transaction

from core.audiobook_models import AudioContributor


def merge_audio_contributors(target, sources):
    """Przepina wszystkie odwołania ze źródeł na cel, uzupełnia pusty e-mail celu i usuwa źródła.

    Relacje bierze z _meta.related_objects, więc nowy model z FK do spisu nie zostanie pominięty
    (relację wiele-do-wielu przypisanie atrybutu odrzuci błędem – całość się wycofa).
    Zapis celu na końcu odświeża kopie imienia i e-maila w audiobookach. Zwraca
    {etykieta relacji: [pk przepiętych rekordów]} – tylko niepuste.
    """
    sources = [person for person in sources if person.pk != target.pk]
    ids = [person.pk for person in sources]
    moved = {}
    with transaction.atomic():
        for relation in AudioContributor._meta.related_objects:
            model, name = relation.related_model, relation.field.name
            label = f'{model._meta.label}.{name}'
            rows = list(model.objects.select_for_update().filter(**{f'{name}__in': ids}).order_by('pk'))
            for row in rows:
                setattr(row, name, target)
                # save() modelu (np. Audiobook) odświeża kopie danych osoby.
                row.save(update_fields=[name])
            if rows:
                moved[label] = [row.pk for row in rows]
        if not target.email:
            target.email = next((person.email for person in sources if person.email), '')
        # Źródła najpierw usuwamy – para (imię, e-mail) jest unikalna.
        AudioContributor.objects.filter(pk__in=ids).delete()
        target.save()
    return moved
