from django.contrib import admin
from django.urls import include, path
from core.admin_urls import polish_admin_patterns


urlpatterns = [
    path(
        "panel/",
        (polish_admin_patterns(admin.site.get_urls()), "admin", admin.site.name),
    ),
    path(
        "konto/",
        include("core.auth_urls"),
    ),
    path(
        "ilustracje/",
        include("illustrations.urls"),
    ),
    path(
        "",
        include("core.urls"),
    ),
]
