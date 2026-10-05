"""Project-owned admin customization; Django base classes remain unchanged."""
from copy import copy
from django.contrib.admin import AdminSite, ModelAdmin
from core.admin_access import SuperuserAdminAuthenticationForm
from core.admin_people import PersonAutocompleteJsonView, PeopleChoiceMixin
from core.admin_navigation import grouped_app_list, blacklist_index
from django.urls import path


class CMSModelAdminMixin(PeopleChoiceMixin):
    def get_changelist(self, request, **kwargs):
        base = super().get_changelist(request, **kwargs)

        class PageSizeChangeList(base):
            def get_filters_params(self, params=None):
                values = super().get_filters_params(params)
                values.pop('page_size', None)
                return values

        return PageSizeChangeList

    def get_changelist_instance(self, request):
        from core.pagination import get_page_size, ALLOWED_PAGE_SIZES
        scoped = copy(self)
        default = self.list_per_page if self.list_per_page in ALLOWED_PAGE_SIZES else 25
        scoped.list_per_page = get_page_size(request, default=default)
        cl = super(CMSModelAdminMixin, scoped).get_changelist_instance(request)
        cl.cms_size_options = [{'size': size, 'url': cl.get_query_string({'page_size': size, 'p': None, 'all': None})} for size in sorted(ALLOWED_PAGE_SIZES)]
        cl.cms_has_previous = not cl.show_all and cl.page_num > 1
        cl.cms_has_next = not cl.show_all and cl.page_num < cl.paginator.num_pages
        cl.cms_first_url = cl.get_query_string({'p': 1}, ['all'])
        cl.cms_previous_url = cl.get_query_string({'p': max(1, cl.page_num - 1)}, ['all'])
        cl.cms_next_url = cl.get_query_string({'p': min(cl.paginator.num_pages, cl.page_num + 1)}, ['all'])
        cl.cms_last_url = cl.get_query_string({'p': cl.paginator.num_pages}, ['all'])
        return cl

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
