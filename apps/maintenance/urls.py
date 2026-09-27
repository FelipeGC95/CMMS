from django.urls import path

from . import views

urlpatterns = [
    path("plans/", views.plan_list_view, name="plan_list"),
    path("plans/new/", views.plan_create_view, name="plan_create"),
    path("plans/settings/", views.maintenance_settings_view, name="maintenance_settings"),
    path("plans/<uuid:pk>/", views.plan_detail_view, name="plan_detail"),
    path("plans/<uuid:pk>/draft/", views.plan_draft_view, name="plan_draft"),
    path("plans/<uuid:pk>/draft/start/", views.plan_draft_start_view, name="plan_draft_start"),
    path("plans/<uuid:pk>/draft/publish/", views.plan_publish_view, name="plan_publish"),
    path("plans/<uuid:pk>/draft/discard/", views.plan_discard_view, name="plan_discard"),
    path("plans/<uuid:pk>/draft/items/<uuid:item_pk>/", views.plan_item_edit_view, name="plan_item_edit"),
    path(
        "plans/<uuid:pk>/draft/items/<uuid:item_pk>/remove/",
        views.plan_item_remove_view,
        name="plan_item_remove",
    ),
    path(
        "plans/<uuid:pk>/draft/items/<uuid:item_pk>/move/", views.plan_item_move_view, name="plan_item_move"
    ),
    path(
        "plans/<uuid:pk>/draft/resources/<uuid:resource_pk>/",
        views.plan_resource_edit_view,
        name="plan_resource_edit",
    ),
    path(
        "plans/<uuid:pk>/draft/resources/<uuid:resource_pk>/remove/",
        views.plan_resource_remove_view,
        name="plan_resource_remove",
    ),
    path("plans/<uuid:pk>/assign/", views.plan_assign_view, name="plan_assign"),
    path("plans/<uuid:pk>/assign/class/", views.plan_assign_class_view, name="plan_assign_class"),
    path(
        "plans/<uuid:pk>/assignments/<uuid:assignment_pk>/deactivate/",
        views.assignment_deactivate_view,
        name="plan_assignment_deactivate",
    ),
    path("assets/<uuid:pk>/plans/assign/", views.asset_plan_assign_view, name="asset_plan_assign"),
]
