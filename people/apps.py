from django.apps import AppConfig


class PeopleConfig(AppConfig):
    name = 'people'
    verbose_name = "Ludzie"

    def ready(self):
        from django.db.models.signals import pre_delete, pre_save
        from .models import Person, Role, remember_person_row, remember_role_people
        # Najpierw poprzedni stan rekordu (jeden odczyt), z którego korzystają odbiorcy z poniższych modułów.
        pre_save.connect(remember_person_row, sender=Person, dispatch_uid="people-person-previous-row")
        pre_save.connect(remember_role_people, sender=Role, dispatch_uid="people-role-previous-people")
        pre_delete.connect(remember_role_people, sender=Role, dispatch_uid="people-role-deleted-people")
        from . import dropbox_access, identity, signals  # noqa: F401
