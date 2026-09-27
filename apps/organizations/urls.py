from django.urls import path

from . import views

urlpatterns = [
    path("settings/", views.organization_settings_view, name="organization_settings"),
    path("org-theme.css", views.organization_theme_css_view, name="organization_theme_css"),
    path("settings/logo.png", views.organization_logo_view, name="organization_logo"),
    path("settings/export/", views.data_export_request_view, name="data_export_request"),
    path("settings/export/<uuid:pk>/", views.data_export_download_view, name="data_export_download"),
    path("settings/delete/", views.organization_delete_view, name="organization_delete"),
    path("settings/delete/cancel/", views.organization_delete_cancel_view, name="organization_delete_cancel"),
    path("locations/", views.location_list_view, name="location_list"),
    path("locations/<uuid:pk>/", views.location_edit_view, name="location_edit"),
    path("locations/<uuid:pk>/delete/", views.location_delete_view, name="location_delete"),
]
