"""Endpoints de la API v1 (X-05). Delgados: validan y llaman a los mismos servicios que la web."""

import uuid

from django.db.models import Q
from django.utils.translation import gettext as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.accounts.permissions import Perm, require_perm
from apps.assets import services as asset_services
from apps.assets.models import Asset, Meter, MeterReading
from apps.assets.reading_imports import apply_reading_import, build_reading_preview
from apps.assets.selectors import visible_assets
from apps.organizations.models import Location
from apps.organizations.selectors import descendant_ids
from apps.work import services as work_services
from apps.work.models import WorkOrder, WorkRequest
from apps.work.selectors import visible_requests, work_order_list

from . import serializers as s
from .framework import TenantAPIMixin
from .services import create_work_order_idempotent


def _param(name, description, type_=OpenApiTypes.STR, enum=None):
    return OpenApiParameter(name, type_, OpenApiParameter.QUERY, description=description, enum=enum)


def _uuid(request, name):
    raw = request.query_params.get(name, "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ValidationError({name: [_("Identificador inválido.")]}) from None


class BaseViewSet(TenantAPIMixin, viewsets.GenericViewSet):
    lookup_value_converter = "uuid"


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param("code", "Código exacto (sin distinguir mayúsculas)."),
            _param("search", "Texto en código o nombre."),
            _param("location", "Ubicación (incluye sus hijas).", OpenApiTypes.UUID),
            _param("asset_class", "Clase de activo.", OpenApiTypes.UUID),
            _param("status", "Estado.", enum=[c for c, _l in Asset.Status.choices]),
        ]
    ),
    create=extend_schema(request=s.AssetWriteSerializer, responses={201: s.AssetSerializer}),
    partial_update=extend_schema(request=s.AssetWriteSerializer, responses=s.AssetSerializer),
)
class AssetViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    BaseViewSet,
):
    """Activos visibles de la organización (sin dados de baja)."""

    serializer_class = s.AssetSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = (
            visible_assets(membership=self.membership)
            .filter(is_active=True)
            .select_related("asset_class", "location")
            .order_by("code")
        )
        if self.action != "list":
            return qs
        params = self.request.query_params
        if code := params.get("code", "").strip():
            qs = qs.filter(code__iexact=code)
        if search := params.get("search", "").strip():
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        if location_id := _uuid(self.request, "location"):
            location_ids = {location_id, *descendant_ids_by_pk(location_id)}
            qs = qs.filter(location_id__in=location_ids)
        if class_id := _uuid(self.request, "asset_class"):
            qs = qs.filter(asset_class_id=class_id)
        if status_ := params.get("status", "").strip():
            qs = qs.filter(status=status_)
        return qs

    def create(self, request, *args, **kwargs):
        data = self._validated(request.data)
        asset = asset_services.create_asset(membership=self.membership, **data)
        return Response(s.AssetSerializer(asset).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        asset = self.get_object()
        current = {
            "code": asset.code,
            "name": asset.name,
            "asset_class": asset.asset_class,
            "location": asset.location,
            "parent": asset.parent,
            "criticality": asset.criticality,
            "manufacturer": asset.manufacturer,
            "model": asset.model,
            "serial_number": asset.serial_number,
            "year": asset.year,
            "in_service_on": asset.in_service_on,
        }
        data = self._validated(request.data, partial=True)
        attributes = data.pop("attributes", None)
        asset = asset_services.update_asset(
            membership=self.membership, asset=asset, **{**current, **data}, attributes=attributes
        )
        return Response(s.AssetSerializer(asset).data)

    def _validated(self, data, partial=False):
        serializer = s.AssetWriteSerializer(data=data, partial=partial, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        return dict(serializer.validated_data)


def descendant_ids_by_pk(location_id):
    # Solo recorre ubicaciones de la organización del contexto: un id ajeno no tiene hijas.
    return descendant_ids(Location(pk=location_id))


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param("asset", "Activo.", OpenApiTypes.UUID),
            _param("asset_code", "Código del activo."),
        ]
    )
)
class MeterViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, BaseViewSet):
    """Medidores activos de los activos visibles."""

    serializer_class = s.MeterSerializer

    def get_queryset(self):
        qs = Meter.objects.filter(
            is_active=True, asset__in=visible_assets(membership=self.membership).filter(is_active=True)
        ).select_related("asset")
        if asset_id := _uuid(self.request, "asset"):
            qs = qs.filter(asset_id=asset_id)
        if code := self.request.query_params.get("asset_code", "").strip():
            qs = qs.filter(asset__code__iexact=code)
        return qs.order_by("asset__code", "kind", "name")


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param("meter", "Medidor.", OpenApiTypes.UUID),
            _param("asset", "Activo.", OpenApiTypes.UUID),
        ]
    ),
    create=extend_schema(request=s.MeterReadingCreateSerializer, responses={201: s.MeterReadingSerializer}),
)
class MeterReadingViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, BaseViewSet):
    """Lecturas de medidor (AST-07). Crear es idempotente por `client_uuid`."""

    serializer_class = s.MeterReadingSerializer

    def get_queryset(self):
        qs = MeterReading.objects.filter(
            meter__asset__in=visible_assets(membership=self.membership).filter(is_active=True)
        )
        if meter_id := _uuid(self.request, "meter"):
            qs = qs.filter(meter_id=meter_id)
        if asset_id := _uuid(self.request, "asset"):
            qs = qs.filter(meter__asset_id=asset_id)
        return qs.order_by("-read_at")

    def create(self, request, *args, **kwargs):
        serializer = s.MeterReadingCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        reading, _warnings = asset_services.record_meter_reading(
            membership=self.membership, source=MeterReading.Source.API, **serializer.validated_data
        )
        return Response(s.MeterReadingSerializer(reading).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=s.BulkReadingSerializer, responses=s.BulkReadingResultSerializer)
    @action(detail=False, methods=["post"])
    def bulk(self, request):
        """Carga masiva (AST-09): mismas reglas que el CSV; idempotente por (medidor, fecha, valor)."""
        require_perm(self.membership, Perm.METER_READING_CREATE)
        serializer = s.BulkReadingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_rows = [{**row, "_row": i} for i, row in enumerate(serializer.validated_data["rows"], start=1)]
        preview = build_reading_preview(raw_rows, membership=self.membership)
        result = apply_reading_import(
            [r.__dict__ for r in preview.valid_rows],
            membership=self.membership,
            source=MeterReading.Source.API,
        )
        failed = dict(result.errors)
        for row in preview.rows:
            if row.row in failed:
                row.action, row.errors = "error", [failed[row.row]]
        body = {
            "created": result.created,
            "duplicates": preview.count("duplicate") + result.duplicates,
            "errors": preview.count("error"),
            "rows": preview.rows,
        }
        return Response(s.BulkReadingResultSerializer(body).data)


WO_STATUSES = [c for c, _l in WorkOrder.Status.choices]


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param(
                "scope", "open, overdue, done o all (por defecto).", enum=["all", "open", "overdue", "done"]
            ),
            _param("status", "Estado.", enum=WO_STATUSES),
            _param("work_type", "Tipo.", enum=[c for c, _l in WorkOrder.Type.choices]),
            _param("asset", "Activo.", OpenApiTypes.UUID),
            _param("location", "Ubicación (incluye sus hijas).", OpenApiTypes.UUID),
        ]
    ),
    create=extend_schema(request=s.WorkOrderCreateSerializer, responses={201: s.WorkOrderSerializer}),
)
class WorkOrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, BaseViewSet):
    """Órdenes de trabajo. Crear es idempotente por `client_uuid`."""

    serializer_class = s.WorkOrderSerializer

    def get_queryset(self):
        if self.action != "list":
            return work_order_list(membership=self.membership, scope="all")
        params = self.request.query_params
        asset = location = None
        if asset_id := _uuid(self.request, "asset"):
            asset = visible_assets(membership=self.membership).filter(pk=asset_id).first()
            if asset is None:
                return WorkOrder.objects.none()
        if location_id := _uuid(self.request, "location"):
            location = Location.objects.filter(pk=location_id).first()
            if location is None:
                return WorkOrder.objects.none()
        return work_order_list(
            membership=self.membership,
            scope=params.get("scope", "all"),
            status=params.get("status", ""),
            work_type=params.get("work_type", ""),
            asset=asset,
            location=location,
        )

    def create(self, request, *args, **kwargs):
        serializer = s.WorkOrderCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        work_order = create_work_order_idempotent(membership=self.membership, **serializer.validated_data)
        return Response(s.WorkOrderSerializer(work_order).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=s.TransitionSerializer, responses=s.WorkOrderSerializer)
    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """Cambia el estado (ciclo de vida WO-02). Repetir el mismo destino no hace nada."""
        work_order = self.get_object()
        serializer = s.TransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if work_order.status != serializer.validated_data["to_status"]:
            work_order = work_services.transition(
                work_order=work_order, membership=self.membership, **serializer.validated_data
            )
        return Response(s.WorkOrderSerializer(work_order).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[_param("status", "Estado.", enum=[c for c, _l in WorkRequest.Status.choices])]
    ),
    create=extend_schema(request=s.WorkRequestCreateSerializer, responses={201: s.WorkRequestSerializer}),
)
class WorkRequestViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, BaseViewSet):
    """Avisos de falla (REQ-01): crear genera su OT correctiva; idempotente por `client_uuid`."""

    serializer_class = s.WorkRequestSerializer

    def get_queryset(self):
        qs = visible_requests(membership=self.membership).select_related("work_order")
        if status_ := self.request.query_params.get("status", "").strip():
            qs = qs.filter(status=status_)
        return qs.order_by("-created_at")

    def create(self, request, *args, **kwargs):
        serializer = s.WorkRequestCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        work_request = work_services.report_failure(membership=self.membership, **serializer.validated_data)
        return Response(s.WorkRequestSerializer(work_request).data, status=status.HTTP_201_CREATED)
