from django.urls import path

from . import views

urlpatterns = [
    path("assets/export.xlsx", views.asset_list_xlsx_view, name="asset_export_xlsx"),
    path("work-orders/export.xlsx", views.work_order_list_xlsx_view, name="work_order_export_xlsx"),
]
