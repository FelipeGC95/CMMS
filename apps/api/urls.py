from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerSplitView
from rest_framework.routers import DefaultRouter

from . import field_views, v1, views

router = DefaultRouter(trailing_slash=True, use_regex_path=False)
router.include_root_view = False
router.register("assets", v1.AssetViewSet, basename="api-asset")
router.register("meters", v1.MeterViewSet, basename="api-meter")
router.register("readings", v1.MeterReadingViewSet, basename="api-reading")
router.register("work-orders", v1.WorkOrderViewSet, basename="api-work-order")
router.register("requests", v1.WorkRequestViewSet, basename="api-request")
# Inventario (INV-17): además del módulo `api`, exigen `inventory` (API_MODULES).
router.register("parts", v1.PartViewSet, basename="api-part")
router.register("warehouses", v1.WarehouseViewSet, basename="api-warehouse")
router.register("stock", v1.StockViewSet, basename="api-stock")
router.register("stock-movements", v1.StockMovementViewSet, basename="api-stock-movement")

# API de terreno (X-06, ADR-011): sesión de la web, solo para la PWA del técnico.
field_router = DefaultRouter(trailing_slash=True, use_regex_path=False)
field_router.include_root_view = False
field_router.register("work-orders", field_views.FieldWorkOrderViewSet, basename="field-work-order")
field_router.register("meters", field_views.FieldMeterViewSet, basename="field-meter")
field_router.register("readings", field_views.FieldMeterReadingViewSet, basename="field-reading")
field_router.register("requests", field_views.FieldWorkRequestViewSet, basename="field-request")

field_urlpatterns = [
    path("bootstrap/", field_views.BootstrapView.as_view(), name="field_api_bootstrap"),
    path("snapshot/", field_views.SnapshotView.as_view(), name="field_api_snapshot"),
    path("reference/", field_views.ReferenceView.as_view(), name="field_api_reference"),
    *field_router.urls,
]

urlpatterns = [
    path("api/v1/", include(router.urls)),
    path("field/api/", include(field_urlpatterns)),
    path("api/schema/", SpectacularAPIView.as_view(), name="api_schema"),
    # Swagger UI desde estáticos locales (sidecar) y con el script de inicio en un archivo
    # aparte: sin CDN ni JS en línea (CSP estricta).
    path("api/docs/", SpectacularSwaggerSplitView.as_view(url_name="api_schema"), name="api_docs"),
    path("settings/api-tokens/", views.api_token_list_view, name="api_token_list"),
    path("settings/api-tokens/<uuid:pk>/revoke/", views.api_token_revoke_view, name="api_token_revoke"),
]
