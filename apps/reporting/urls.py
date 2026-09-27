from django.urls import path

from . import views

urlpatterns = [
    path("assets/export.xlsx", views.asset_list_xlsx_view, name="asset_export_xlsx"),
    path("work-orders/export.xlsx", views.work_order_list_xlsx_view, name="work_order_export_xlsx"),
    path("assets/<uuid:pk>/sheet.pdf", views.asset_pdf_view, name="asset_pdf"),
    path("work-orders/<uuid:pk>/sheet.pdf", views.work_order_pdf_view, name="work_order_pdf"),
]
