"""URL de inventario desde los planes (INV-11), bajo el prefijo inventory/from-plans/."""

from django.urls import path

from . import plan_views

urlpatterns = [
    path("", plan_views.from_plans_view, name="inventory_from_plans"),
    path("confirm/", plan_views.from_plans_confirm_view, name="inventory_from_plans_confirm"),
    path("parts/search/", plan_views.part_search_view, name="inventory_plan_part_search"),
]
