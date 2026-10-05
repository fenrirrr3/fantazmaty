from django.db import transaction
from authors.models import Author

with transaction.atomic():
    author = Author.objects.select_for_update().get(pk=19)
    if author.last_name != "Biskup" or author.first_name not in ("Agata", "Agnieszka"):
        raise RuntimeError("Autor ID 19 ma inne dane niż oczekiwano. Nie zmieniono rekordu.")
    if Author.objects.filter(first_name__iexact="Agnieszka", last_name__iexact="Biskup").exclude(pk=19).exists():
        raise RuntimeError("Istnieje już inna Agnieszka Biskup. Nie zmieniono rekordu; potrzebne rozstrzygnięcie duplikatu.")
    if author.first_name == "Agata":
        author.first_name = "Agnieszka"
        author.save(update_fields=["first_name"])
        print("Zmieniono autorkę ID 19: Agata Biskup -> Agnieszka Biskup. Powiązania zachowano.")
    else:
        print("Autorka ID 19 ma już imię Agnieszka — bez zmian.")
