from django.contrib.admin.apps import AdminConfig


class CMSAdminConfig(AdminConfig):
    default_site = 'core.admin_site.CMSAdminSite'
