from django.urls import path

from . import views

urlpatterns = [
    path("reports/kpis/", views.kpi_report_view, name="kpi_report"),
    path("reports/kpis/export.xlsx", views.kpi_report_xlsx_view, name="kpi_report_export_xlsx"),
    path("assets/export.xlsx", views.asset_list_xlsx_view, name="asset_export_xlsx"),
    path("work-orders/export.xlsx", views.work_order_list_xlsx_view, name="work_order_export_xlsx"),
    path("work-orders/backlog/export.xlsx", views.backlog_xlsx_view, name="work_order_backlog_export_xlsx"),
    path("assets/<uuid:pk>/sheet.pdf", views.asset_pdf_view, name="asset_pdf"),
    path("work-orders/<uuid:pk>/sheet.pdf", views.work_order_pdf_view, name="work_order_pdf"),
]
