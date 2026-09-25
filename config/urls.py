from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.urls import include, path

from apps.accounts.views import switch_organization
from apps.assets import import_views as asset_import_views
from apps.assets import views as asset_views
from apps.reporting.views import dashboard
from apps.ui.views import design_system_css_view, design_system_view
from apps.work import views as work_views

admin.site.site_header = "GALBUST CMMS · Administración"
admin.site.site_title = "GALBUST CMMS"
# El admin entra por el login de allauth (límite de intentos y correo verificado).
admin.site.login = login_required(admin.site.login)


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("healthz", healthz, name="healthz"),
    path("accounts/", include("allauth.urls")),
    path("org/switch/", switch_organization, name="switch_organization"),
    path("assets/", asset_views.asset_list_view, name="asset_list"),
    path("assets/labels/", asset_views.asset_labels_view, name="asset_labels"),
    path("assets/import/", asset_import_views.asset_import_view, name="asset_import"),
    path(
        "assets/import/template.xlsx",
        asset_import_views.asset_import_template_view,
        name="asset_import_template",
    ),
    path(
        "assets/import/<str:token>/confirm/",
        asset_import_views.asset_import_confirm_view,
        name="asset_import_confirm",
    ),
    path("assets/<uuid:pk>/", asset_views.asset_detail_view, name="asset_detail"),
    path("assets/<uuid:pk>/report/", asset_views.asset_report_view, name="asset_report"),
    path(
        "assets/<uuid:pk>/meters/<uuid:meter_pk>/readings/",
        asset_views.meter_reading_view,
        name="meter_reading",
    ),
    path("q/<str:token>/", asset_views.asset_qr_view, name="asset_qr"),
    path("work-orders/", work_views.work_order_list_view, name="work_order_list"),
    path("work-orders/<uuid:pk>/", work_views.work_order_detail_view, name="work_order_detail"),
    path(
        "work-orders/<uuid:pk>/transition/",
        work_views.work_order_transition_view,
        name="work_order_transition",
    ),
    path(
        "work-orders/<uuid:pk>/complete/",
        work_views.work_order_complete_view,
        name="work_order_complete",
    ),
    path(
        "work-orders/<uuid:pk>/items/<uuid:item_pk>/",
        work_views.checklist_answer_view,
        name="checklist_answer",
    ),
    path("files/<uuid:pk>/", work_views.attachment_view, name="attachment"),
    path("design-system/", design_system_view, name="design_system"),
    path("design-system/swatches.css", design_system_css_view, name="design_system_css"),
    path("admin/", admin.site.urls),
]
