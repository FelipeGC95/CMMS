from django.urls import path

from . import views

urlpatterns = [
    path("assets/<uuid:pk>/manuals/", views.asset_manual_add_view, name="asset_manual_add"),
    path(
        "asset-classes/<uuid:pk>/manuals/", views.asset_class_manual_add_view, name="asset_class_manual_add"
    ),
    path("manuals/<uuid:pk>/remove/", views.manual_remove_view, name="manual_remove"),
    path("manuals/search/", views.manual_search_view, name="manual_search"),
]
