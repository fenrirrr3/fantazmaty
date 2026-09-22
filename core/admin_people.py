"""Searchable person choices with account names, confined to Django admin."""
from functools import wraps
from types import MethodType
from django.contrib import admin
from django.contrib.admin.options import BaseModelAdmin
from django.contrib.admin.views.autocomplete import AutocompleteJsonView
from django.contrib.admin.widgets import AutocompleteSelect
from django.contrib.auth import get_user_model


def person_label(obj):
    name = ' '.join(part.strip() for part in (obj.first_name, obj.last_name) if part and part.strip())
    return name or f'Osoba bez imienia i nazwiska (ID {obj.pk})'


def is_person_model(model):
    return model is get_user_model() or model._meta.label_lower == 'people.person'


class PersonAutocompleteJsonView(AutocompleteJsonView):
    def serialize_result(self, obj, to_field_name):
        result = super().serialize_result(obj, to_field_name)
        if is_person_model(type(obj)):
            result['text'] = person_label(obj)
        return result


def autocomplete_view(self, request):
    return PersonAutocompleteJsonView.as_view(admin_site=self)(request)


def install():
    admin.site.autocomplete_view = MethodType(autocomplete_view, admin.site)
    original = BaseModelAdmin.formfield_for_foreignkey
    if getattr(original, '_person_choices_installed', False):
        return

    @wraps(original)
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        target = db_field.remote_field.model
        personal = is_person_model(target)
        if personal and self.admin_site.is_registered(target):
            target_admin = self.admin_site.get_model_admin(target)
            if target_admin.get_search_fields(request):
                kwargs['widget'] = AutocompleteSelect(db_field, self.admin_site, using=kwargs.get('using'))
        field = original(self, db_field, request, **kwargs)
        if personal and field is not None:
            field.label_from_instance = person_label
        return field

    formfield_for_foreignkey._person_choices_installed = True
    BaseModelAdmin.formfield_for_foreignkey = formfield_for_foreignkey
