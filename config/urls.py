from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import path

from apps.accounts.views import switch_organization
from apps.assets import views as asset_views
from apps.reporting.views import dashboard
from apps.work import views as work_views

admin.site.site_header = "GALBUST CMMS · Administración"
admin.site.site_title = "GALBUST CMMS"


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("healthz", healthz, name="healthz"),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="accounts/login.html", redirect_authenticated_user=True),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("org/switch/", switch_organization, name="switch_organization"),
    path("assets/", asset_views.asset_list_view, name="asset_list"),
    path("assets/labels/", asset_views.asset_labels_view, name="asset_labels"),
    path("assets/<uuid:pk>/", asset_views.asset_detail_view, name="asset_detail"),
    path("assets/<uuid:pk>/report/", asset_views.asset_report_view, name="asset_report"),
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
    path("admin/", admin.site.urls),
]
