"""Project-owned admin customization; Django base classes remain unchanged."""
from copy import copy
from django.contrib.admin import AdminSite, ModelAdmin
from core.admin_access import SuperuserAdminAuthenticationForm
from core.admin_people import PersonAutocompleteJsonView, PeopleChoiceMixin
from core.admin_navigation import grouped_app_list, blacklist_index
from django.urls import path


class CMSModelAdminMixin(PeopleChoiceMixin):
    def get_inline_instances(self, request, obj=None):
        scoped = copy(self)
        scoped.inlines = [type('CMS' + inline.__name__, (PeopleChoiceMixin, inline), {}) for inline in self.inlines]
        return super(CMSModelAdminMixin, scoped).get_inline_instances(request, obj)


class CMSAdminSite(AdminSite):
    login_form = SuperuserAdminAuthenticationForm
    site_header = 'Fantazmaty – administracja'
    site_title = 'Administracja Fantazmaty'
    index_title = 'Panel administracyjny'

    def has_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def autocomplete_view(self, request):
        return PersonAutocompleteJsonView.as_view(admin_site=self)(request)

    def register(self, model_or_iterable, admin_class=None, **options):
        original = admin_class or ModelAdmin
        if not issubclass(original, CMSModelAdminMixin):
            admin_class = type('CMS' + original.__name__, (CMSModelAdminMixin, original), {'__module__': original.__module__})
        return super().register(model_or_iterable, admin_class, **options)

    def get_urls(self):
        return [path('czarna-lista/', self.admin_view(lambda request: blacklist_index(self, request)), name='blacklist_index')] + super().get_urls()

    def get_app_list(self, request, app_label=None):
        original = super().get_app_list(request, app_label)
        return original if app_label else grouped_app_list(original)
