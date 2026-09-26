from django.urls import path

from . import class_views, views

urlpatterns = [
    path("assets/new/", views.asset_create_view, name="asset_create"),
    path("assets/<uuid:pk>/edit/", views.asset_edit_view, name="asset_edit"),
    path("assets/<uuid:pk>/status/", views.asset_status_view, name="asset_status"),
    path("assets/<uuid:pk>/decommission/", views.asset_decommission_view, name="asset_decommission"),
    path("assets/attribute-fields/", views.asset_attribute_fields_view, name="asset_attribute_fields"),
    path("asset-classes/", class_views.asset_class_list_view, name="asset_class_list"),
    path("asset-classes/<uuid:pk>/", class_views.asset_class_detail_view, name="asset_class_detail"),
    path(
        "asset-classes/<uuid:pk>/attributes/<uuid:attribute_pk>/",
        class_views.attribute_edit_view,
        name="attribute_edit",
    ),
    path("assets/<uuid:pk>/attributes/", class_views.asset_attributes_view, name="asset_attributes"),
]
