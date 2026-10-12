"""Rutas web de inventario (incluidas en config/urls.py bajo `inventory/`)."""

from django.urls import path

from . import import_views, movement_views, views

urlpatterns = [
    path("", views.inventory_home_view, name="inventory_home"),
    path("export.xlsx", movement_views.stock_xlsx_view, name="inventory_export_xlsx"),
    path("receive/", movement_views.receive_view, name="inventory_receive"),
    path("count/", movement_views.count_pick_view, name="inventory_count"),
    path("parts/<uuid:pk>/count/", movement_views.part_count_view, name="inventory_part_count"),
    path("transfer/", movement_views.transfer_view, name="inventory_transfer"),
    path("movements/", movement_views.movement_list_view, name="inventory_movements"),
    path("movements/export.xlsx", movement_views.movements_xlsx_view, name="inventory_movements_export_xlsx"),
    path("reorder/", movement_views.reorder_view, name="inventory_reorder"),
    path("reorder/export.xlsx", movement_views.reorder_xlsx_view, name="inventory_reorder_export_xlsx"),
    path("parts/new/", views.part_create_view, name="inventory_part_create"),
    path("parts/search/", views.part_search_view, name="inventory_part_search"),
    path("parts/<uuid:pk>/", views.part_detail_view, name="inventory_part_detail"),
    path("parts/<uuid:pk>/edit/", views.part_edit_view, name="inventory_part_edit"),
    path("parts/<uuid:pk>/levels/", views.part_levels_view, name="inventory_part_levels"),
    path("parts/<uuid:pk>/active/", views.part_active_view, name="inventory_part_active"),
    path("warehouses/", views.warehouse_list_view, name="inventory_warehouse_list"),
    path("warehouses/new/", views.warehouse_create_view, name="inventory_warehouse_create"),
    path("warehouses/<uuid:pk>/edit/", views.warehouse_edit_view, name="inventory_warehouse_edit"),
    path("warehouses/<uuid:pk>/default/", views.warehouse_default_view, name="inventory_warehouse_default"),
    path("warehouses/<uuid:pk>/active/", views.warehouse_active_view, name="inventory_warehouse_active"),
    path("import/", import_views.part_import_view, name="inventory_import"),
    path("import/template.xlsx", import_views.part_import_template_view, name="inventory_import_template"),
    path(
        "import/<str:token>/confirm/", import_views.part_import_confirm_view, name="inventory_import_confirm"
    ),
]
