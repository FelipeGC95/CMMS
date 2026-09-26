from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.urls import include, path

from apps.accounts.views import switch_organization
from apps.assets import import_views as asset_import_views
from apps.assets import views as asset_views
from apps.attachments.views import attachment_view
from apps.reporting.views import dashboard
from apps.ui.views import design_system_css_view, design_system_view, menu_view
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
    path("", include("apps.accounts.urls")),
    path("", include("apps.organizations.urls")),
    path("", include("apps.assets.urls")),
    path("audit/", include("apps.audit.urls")),
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
    path("assets/<uuid:pk>/attachments/", asset_views.asset_attachment_add_view, name="asset_attachment_add"),
    path("work-orders/", work_views.work_order_list_view, name="work_order_list"),
    path("work-orders/new/", work_views.work_order_create_view, name="work_order_create"),
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
    path("work-orders/<uuid:pk>/labor/", work_views.labor_add_view, name="work_order_labor_add"),
    path("work-orders/<uuid:pk>/parts/", work_views.part_add_view, name="work_order_part_add"),
    path(
        "work-orders/<uuid:pk>/other-costs/", work_views.other_cost_add_view, name="work_order_other_cost_add"
    ),
    path(
        "work-orders/<uuid:pk>/costs/<str:kind>/<uuid:line_pk>/delete/",
        work_views.cost_line_delete_view,
        name="work_order_cost_delete",
    ),
    path(
        "work-orders/<uuid:pk>/items/<uuid:item_pk>/",
        work_views.checklist_answer_view,
        name="checklist_answer",
    ),
    path(
        "work-orders/<uuid:pk>/items/<uuid:item_pk>/photos/",
        work_views.checklist_photo_add_view,
        name="checklist_photo_add",
    ),
    path(
        "work-orders/<uuid:pk>/checklist/", work_views.work_order_checklist_view, name="work_order_checklist"
    ),
    path("files/<uuid:pk>/", attachment_view, name="attachment"),
    path("requests/", work_views.request_list_view, name="request_list"),
    path("requests/<uuid:pk>/accept/", work_views.request_accept_view, name="request_accept"),
    path("requests/<uuid:pk>/reject/", work_views.request_reject_view, name="request_reject"),
    path("requests/<uuid:pk>/merge/", work_views.request_merge_view, name="request_merge"),
    path("menu/", menu_view, name="menu"),
    path("failure-codes/", work_views.failure_code_list_view, name="failure_code_list"),
    path("failure-codes/add/", work_views.failure_code_create_view, name="failure_code_create"),
    path("failure-codes/<uuid:pk>/rename/", work_views.failure_code_rename_view, name="failure_code_rename"),
    path("failure-codes/<uuid:pk>/toggle/", work_views.failure_code_toggle_view, name="failure_code_toggle"),
    path("design-system/", design_system_view, name="design_system"),
    path("design-system/swatches.css", design_system_css_view, name="design_system_css"),
    path("admin/", admin.site.urls),
]
