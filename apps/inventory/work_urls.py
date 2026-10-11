"""Rutas de repuestos en la OT, bajo /inventory/work-orders/ (módulo `inventory`)."""

from django.urls import path

from . import work_views

urlpatterns = [
    path("<uuid:pk>/parts/search/", work_views.work_order_part_search_view, name="inventory_wo_part_search"),
    path("<uuid:pk>/reservations/", work_views.work_order_reserve_view, name="inventory_wo_reserve"),
    path(
        "<uuid:pk>/reservations/<uuid:reservation_pk>/release/",
        work_views.work_order_release_view,
        name="inventory_wo_release",
    ),
]
