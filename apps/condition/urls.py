"""Rutas web de mantenimiento por condición (incluidas en config/urls.py bajo `condition/`)."""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.alert_list_view, name="condition_alert_list"),
    # Nombre fijo: lo usa el aviso `condition_alert` (apps/notifications/services.py).
    path("alerts/<uuid:pk>/", views.alert_detail_view, name="condition_alert_detail"),
    path("alerts/<uuid:pk>/acknowledge/", views.alert_acknowledge_view, name="condition_alert_acknowledge"),
    path("rules/", views.rule_list_view, name="condition_rule_list"),
    path("rules/new/", views.rule_create_view, name="condition_rule_create"),
    path("rules/<uuid:pk>/edit/", views.rule_edit_view, name="condition_rule_edit"),
    path("rules/<uuid:pk>/active/", views.rule_active_view, name="condition_rule_active"),
]
