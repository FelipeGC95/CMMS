from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerSplitView
from rest_framework.routers import DefaultRouter

from . import v1, views

router = DefaultRouter(trailing_slash=True, use_regex_path=False)
router.include_root_view = False
router.register("assets", v1.AssetViewSet, basename="api-asset")
router.register("meters", v1.MeterViewSet, basename="api-meter")
router.register("readings", v1.MeterReadingViewSet, basename="api-reading")
router.register("work-orders", v1.WorkOrderViewSet, basename="api-work-order")
router.register("requests", v1.WorkRequestViewSet, basename="api-request")

urlpatterns = [
    path("api/v1/", include(router.urls)),
    path("api/schema/", SpectacularAPIView.as_view(), name="api_schema"),
    # Swagger UI desde estáticos locales (sidecar) y con el script de inicio en un archivo
    # aparte: sin CDN ni JS en línea (CSP estricta).
    path("api/docs/", SpectacularSwaggerSplitView.as_view(url_name="api_schema"), name="api_docs"),
    path("settings/api-tokens/", views.api_token_list_view, name="api_token_list"),
    path("settings/api-tokens/<uuid:pk>/revoke/", views.api_token_revoke_view, name="api_token_revoke"),
]
