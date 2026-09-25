from django.urls import path

from . import class_views

urlpatterns = [
    path("asset-classes/", class_views.asset_class_list_view, name="asset_class_list"),
    path("asset-classes/<uuid:pk>/", class_views.asset_class_detail_view, name="asset_class_detail"),
    path(
        "asset-classes/<uuid:pk>/attributes/<uuid:attribute_pk>/",
        class_views.attribute_edit_view,
        name="attribute_edit",
    ),
    path("assets/<uuid:pk>/attributes/", class_views.asset_attributes_view, name="asset_attributes"),
]
