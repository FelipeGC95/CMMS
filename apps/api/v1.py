"""Endpoints de la API v1 (X-05). Delgados: validan y llaman a los mismos servicios que la web."""

import uuid

from django.db import transaction
from django.db.models import F, Q
from django.utils.translation import gettext as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from apps.accounts.permissions import Perm, has_perm, require_perm
from apps.assets import services as asset_services
from apps.assets.models import Asset, AssetClass, Meter, MeterReading
from apps.assets.reading_imports import apply_reading_import, build_reading_preview
from apps.assets.selectors import visible_assets
from apps.condition import services as condition_services
from apps.condition.models import ConditionAlert, ConditionRule
from apps.condition.selectors import alert_list, rule_for, rule_list
from apps.inventory import services as inventory_services
from apps.inventory.models import Part, StockItem, StockMovement, Warehouse
from apps.inventory.selectors import visible_warehouses, warehouse_scope
from apps.organizations.models import Location
from apps.organizations.selectors import descendant_ids, visible_locations
from apps.work import services as work_services
from apps.work.forms import CompleteWorkOrderForm, decode_signature
from apps.work.models import WorkOrder, WorkRequest
from apps.work.selectors import (
    checklist_with_photo_count,
    visible_requests,
    work_order_attachments,
    work_order_list,
)

from . import serializers as s
from .framework import TenantAPIMixin
from .services import (
    add_part_usage_idempotent,
    answer_checklist_item_idempotent,
    complete_work_order_idempotent,
    create_condition_rule_idempotent,
    create_work_order_idempotent,
    record_stock_movement_idempotent,
    transition_work_order_idempotent,
    upload_checklist_photo_idempotent,
    upload_work_order_photo_idempotent,
)


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


class ReadingCreateMixin:
    """Alta de una lectura de medidor (AST-07), idempotente por `client_uuid` (v1 y terreno)."""

    def create(self, request, *args, **kwargs):
        serializer = s.MeterReadingCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        reading, _warnings = asset_services.record_meter_reading(
            membership=self.membership, source=self.reading_source, **serializer.validated_data
        )
        return Response(s.MeterReadingSerializer(reading).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param("meter", "Medidor.", OpenApiTypes.UUID),
            _param("asset", "Activo.", OpenApiTypes.UUID),
        ]
    ),
    create=extend_schema(request=s.MeterReadingCreateSerializer, responses={201: s.MeterReadingSerializer}),
)
class MeterReadingViewSet(ReadingCreateMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, BaseViewSet):
    """Lecturas de medidor (AST-07). Crear es idempotente por `client_uuid`."""

    reading_source = MeterReading.Source.API
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


class WorkOrderActionsMixin:
    """Acciones sobre una OT compartidas por la API v1 y la de terreno (`/field/api/`).

    El queryset (`get_queryset`) ya limita a las OT visibles para la membresía.
    """

    transition_serializer_class = s.TransitionSerializer
    part_usage_serializer_class = s.PartUsageCreateSerializer
    # La v1 deja reabrir una OT completada (como la web); la API de terreno no.
    allow_reopen = True

    @extend_schema(request=s.TransitionSerializer, responses=s.WorkOrderSerializer)
    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """Cambia el estado (ciclo de vida WO-02). Repetir el mismo destino no hace nada.

        Idempotente por `client_uuid`; `at` es la hora real del cambio (cliente sin conexión).
        """
        work_order = self.get_object()
        serializer = self.transition_serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        work_order = transition_work_order_idempotent(
            work_order=work_order,
            membership=self.membership,
            reopen=self.allow_reopen,
            **serializer.validated_data,
        )
        return Response(s.WorkOrderSerializer(work_order).data)

    @extend_schema(request=s.WorkOrderCompleteSerializer, responses=s.WorkOrderSerializer)
    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """Completa la OT (WO-03, REQ-03) con las mismas reglas que la web.

        Idempotente por `client_uuid`: reenviarlo devuelve el estado actual sin repetir el cierre.
        """
        work_order = self.get_object()
        serializer = s.WorkOrderCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        client_uuid = data.pop("client_uuid", None)
        at = data.pop("at", None)
        form = _complete_form(work_order, data)
        if not form.is_valid():
            raise ValidationError(
                {("non_field_errors" if k == "__all__" else k): list(v) for k, v in form.errors.items()}
            )
        fields = {name: form.cleaned_data.get(name) for name in COMPLETE_FIELDS}
        fields["notes"] = form.cleaned_data["notes"]
        work_order = complete_work_order_idempotent(
            membership=self.membership,
            work_order=work_order,
            client_uuid=client_uuid,
            at=at,
            reopen=self.allow_reopen,
            **fields,
        )
        return Response(s.WorkOrderSerializer(work_order).data)

    @extend_schema(
        request=s.ChecklistAnswerSerializer,
        responses=s.ChecklistAnswerResultSerializer,
        parameters=[OpenApiParameter("item_id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
    )
    @action(detail=True, methods=["post"], url_path="checklist-items/<uuid:item_id>/answer")
    def answer(self, request, pk=None, item_id=None):
        """Responde un ítem del checklist (WO-03 CA2); reenviar la misma respuesta no cambia nada.

        Con `answered_at` gana la última: una respuesta más antigua que la vigente no se
        aplica y vuelve con `superseded: true`.

        Un ítem «foto obligatoria» se responde subiendo la foto (`…/photos/`); después,
        responderlo solo guarda la observación. Sin foto → 400.
        """
        item = self._checklist_item(item_id)
        serializer = s.ChecklistAnswerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        signature = decode_signature(data.pop("signature", ""))
        item = answer_checklist_item_idempotent(
            membership=self.membership, item=item, signature=signature, **data
        )
        return Response(s.ChecklistAnswerResultSerializer(item).data)

    @extend_schema(responses=s.ChecklistItemListSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="checklist-items")
    def checklist_items(self, request, pk=None):
        """Ítems del checklist de la OT, en orden, con cuántas fotos tiene cada uno."""
        items = checklist_with_photo_count(work_order=self.get_object())
        page = self.paginate_queryset(items)
        return self.get_paginated_response(s.ChecklistItemListSerializer(page, many=True).data)

    @extend_schema(
        methods=["GET"],
        responses=s.PartUsageSerializer(many=True),
        description="Repuestos y materiales usados en la OT, en orden de registro.",
    )
    @extend_schema(
        methods=["POST"],
        request=s.PartUsageCreateSerializer,
        responses={201: s.PartUsageSerializer, 200: s.PartUsageSerializer},
        description="Registra un repuesto usado (INV-09): del catálogo (`part`, descuenta stock "
        "y reserva al costo promedio vigente; requiere el módulo de inventario) o texto libre "
        "(`description`). Idempotente por `client_uuid`: reenviarlo devuelve la línea ya creada "
        "(200); usarlo en otra OT → 409.",
    )
    @action(detail=True, methods=["get", "post"])
    def parts(self, request, pk=None):
        work_order = self.get_object()
        context = {**self.get_serializer_context(), "show_costs": show_inventory_costs(self.membership)}
        if request.method == "GET":
            usages = work_order.parts.select_related("part", "stock_movement").order_by("created_at")
            page = self.paginate_queryset(usages)
            return self.get_paginated_response(s.PartUsageSerializer(page, many=True, context=context).data)
        serializer = self.part_usage_serializer_class(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        usage, created = add_part_usage_idempotent(
            membership=self.membership, work_order=work_order, **serializer.validated_data
        )
        return Response(
            s.PartUsageSerializer(usage, context=context).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(responses=s.AttachmentSerializer(many=True))
    @action(detail=True, methods=["get"])
    def attachments(self, request, pk=None):
        """Adjuntos de la OT y de sus ítems (metadatos). Sin URL: el archivo no se sirve por la API."""
        attachments = work_order_attachments(work_order=self.get_object())
        page = self.paginate_queryset(attachments)
        return self.get_paginated_response(s.AttachmentSerializer(page, many=True).data)

    @extend_schema(
        request={"multipart/form-data": s.PhotoUploadSerializer},
        responses={201: s.AttachmentSerializer, 200: s.AttachmentSerializer},
    )
    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser])
    def photos(self, request, pk=None):
        """Sube una foto a la OT (201). Idempotente por `client_uuid`: reenviarlo devuelve la
        foto ya subida (200); usarlo en otro objeto → 409."""
        work_order = self.get_object()
        data = self._photo_upload(request)
        photo, created = upload_work_order_photo_idempotent(
            membership=self.membership, work_order=work_order, **data
        )
        return self._photo_response(photo, created)

    @extend_schema(
        request={"multipart/form-data": s.PhotoUploadSerializer},
        responses={201: s.AttachmentSerializer, 200: s.AttachmentSerializer},
        parameters=[OpenApiParameter("item_id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="checklist-items/<uuid:item_id>/photos",
        parser_classes=[MultiPartParser],
    )
    def item_photos(self, request, pk=None, item_id=None):
        """Sube una foto a un ítem del checklist (201); en un ítem «foto obligatoria» la foto es
        la respuesta y el ítem queda hecho. Idempotente por `client_uuid` como `…/photos/`."""
        item = self._checklist_item(item_id)
        data = self._photo_upload(request)
        photo, created = upload_checklist_photo_idempotent(membership=self.membership, item=item, **data)
        return self._photo_response(photo, created)

    def _checklist_item(self, item_id):
        item = self.get_object().checklist.filter(pk=item_id).select_related("work_order").first()
        if item is None:
            raise NotFound(_("No encontrado."))
        return item

    @staticmethod
    def _photo_upload(request):
        serializer = s.PhotoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return {
            "uploaded_file": serializer.validated_data["file"],
            "client_uuid": serializer.validated_data.get("client_uuid"),
            "taken_at": serializer.validated_data.get("taken_at"),
        }

    @staticmethod
    def _photo_response(photo, created):
        return Response(
            s.AttachmentSerializer(photo).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


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
class WorkOrderViewSet(WorkOrderActionsMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, BaseViewSet):
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
            location = visible_locations(membership=self.membership).filter(pk=location_id).first()
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


# Campos del formulario web de cierre que se pasan tal cual al servicio.
COMPLETE_FIELDS = (
    "labor_minutes",
    "failure_mode",
    "failure_cause",
    "failure_mechanism",
    "detection_method",
    "component",
    "impact",
    "failure_started_at",
    "failure_ended_at",
    "symptom",
    "meter_value",
    "signature",
)


def _complete_form(work_order, data):
    """El formulario de cierre de la web, con la OT y los datos de la API (mismas reglas)."""
    meter = work_services.work_order_meter(work_order)
    plan = work_order.plan_assignment.plan if work_order.plan_assignment_id else None
    return CompleteWorkOrderForm(
        {
            k: ("" if v is None else v.isoformat() if hasattr(v, "isoformat") else str(v))
            for k, v in data.items()
        },
        with_failure=work_order.work_type == WorkOrder.Type.CORRECTIVE and work_order.asset_id is not None,
        meter=meter,
        meter_required=bool(plan and plan.uses_meter and meter),
        require_failure_details=work_order.organization.require_failure_details,
        asset=work_order.asset,
    )


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


# --- Inventario (INV-17): módulo `inventory` del plan (API_MODULES) ---------------------------


def show_inventory_costs(membership) -> bool:
    """Costos de inventario solo con `inventory.cost_view` (Q3)."""
    return has_perm(membership, Perm.INVENTORY_COST_VIEW)


def _flag(request, name) -> bool:
    return request.query_params.get(name, "").strip().lower() in ("1", "true", "yes")


class InventoryViewSet(BaseViewSet):
    """Inventario: exige `inventory.view` y pasa a los serializadores si se ven costos."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        require_perm(self.membership, Perm.INVENTORY_VIEW)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "show_costs": show_inventory_costs(self.membership)}

    def _visible_warehouse_ids(self):
        return Warehouse.objects.filter(warehouse_scope(self.membership)).values("pk")


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param("code", "Código exacto (sin distinguir mayúsculas)."),
            _param("search", "Texto en código, nombre o n° de parte."),
            _param("category", "Categoría exacta (sin distinguir mayúsculas)."),
            _param("include_inactive", "true para incluir los desactivados.", OpenApiTypes.BOOL),
        ]
    ),
    create=extend_schema(
        request=s.PartWriteSerializer,
        responses={201: s.PartSerializer, 200: s.PartSerializer},
        description="Alta idempotente por código: si ya existe un repuesto con ese código (sin "
        "distinguir mayúsculas), responde 200 con el existente sin cambiarlo.",
    ),
    partial_update=extend_schema(request=s.PartWriteSerializer, responses=s.PartSerializer),
)
class PartViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    InventoryViewSet,
):
    """Catálogo de repuestos (INV-01). Costos solo con `inventory.cost_view`."""

    serializer_class = s.PartSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = Part.objects.order_by("code")
        if self.action != "list":
            return qs
        params = self.request.query_params
        if not _flag(self.request, "include_inactive"):
            qs = qs.filter(is_active=True)
        if code := params.get("code", "").strip():
            qs = qs.filter(code__iexact=code)
        if search := params.get("search", "").strip():
            qs = qs.filter(
                Q(code__icontains=search) | Q(name__icontains=search) | Q(manufacturer_ref__icontains=search)
            )
        if category := params.get("category", "").strip():
            qs = qs.filter(category__iexact=category)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = s.PartWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data.pop("is_active", None)
        existing = Part.objects.filter(code__iexact=data["code"].strip()).first()
        if existing is not None:
            return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)
        part = inventory_services.create_part(membership=self.membership, **data)
        return Response(self.get_serializer(part).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        part = self.get_object()
        serializer = s.PartWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        is_active = data.pop("is_active", None)
        if data:
            part = inventory_services.update_part(membership=self.membership, part=part, **data)
        if is_active is not None:
            part = inventory_services.set_part_active(
                membership=self.membership, part=part, is_active=is_active
            )
        return Response(self.get_serializer(part).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[_param("include_inactive", "true para incluir las desactivadas.", OpenApiTypes.BOOL)]
    )
)
class WarehouseViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, InventoryViewSet):
    """Bodegas visibles (INV-03): sin ubicación o con ubicación en el alcance del token."""

    serializer_class = s.WarehouseSerializer

    def get_queryset(self):
        include_inactive = self.action != "list" or _flag(self.request, "include_inactive")
        return visible_warehouses(membership=self.membership, include_inactive=include_inactive)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param("part", "Repuesto.", OpenApiTypes.UUID),
            _param("warehouse", "Bodega.", OpenApiTypes.UUID),
            _param("below_min", "true: solo bajo mínimo (disponible < mínimo).", OpenApiTypes.BOOL),
        ]
    )
)
class StockViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, InventoryViewSet):
    """Stock por repuesto y bodega (INV-04). Solo lectura: cambia con movimientos."""

    serializer_class = s.StockItemSerializer

    def get_queryset(self):
        qs = StockItem.objects.filter(warehouse__in=self._visible_warehouse_ids()).select_related(
            "part", "warehouse"
        )
        if self.action == "list":
            if part_id := _uuid(self.request, "part"):
                qs = qs.filter(part_id=part_id)
            if warehouse_id := _uuid(self.request, "warehouse"):
                qs = qs.filter(warehouse_id=warehouse_id)
            if _flag(self.request, "below_min"):
                qs = qs.filter(min_quantity__gt=0, on_hand__lt=F("reserved") + F("min_quantity"))
        return qs.order_by("part__code", "warehouse__code")


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param("part", "Repuesto.", OpenApiTypes.UUID),
            _param("warehouse", "Bodega.", OpenApiTypes.UUID),
            _param("work_order", "OT.", OpenApiTypes.UUID),
            _param("kind", "Tipo.", enum=[c for c, _l in StockMovement.Kind.choices]),
        ]
    ),
    create=extend_schema(
        request=s.StockMovementCreateSerializer,
        responses={201: s.StockMovementResultSerializer, 200: s.StockMovementResultSerializer},
        description="Registra una entrada, un conteo o una transferencia con los servicios de la "
        "web. Idempotente por `client_uuid`: reenviarlo devuelve lo ya creado (200); usarlo en "
        "otra operación → 409. Un conteo sin diferencia responde 200 sin movimientos.",
    ),
)
class StockMovementViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, InventoryViewSet
):
    """Libro de movimientos de stock (INV-14): inmutable; un error se corrige con otro movimiento."""

    serializer_class = s.StockMovementSerializer

    def get_queryset(self):
        qs = StockMovement.objects.filter(warehouse__in=self._visible_warehouse_ids()).select_related(
            "part", "warehouse"
        )
        if self.action == "list":
            for name in ("part", "warehouse", "work_order"):
                if value := _uuid(self.request, name):
                    qs = qs.filter(**{f"{name}_id": value})
            if kind := self.request.query_params.get("kind", "").strip():
                qs = qs.filter(kind=kind)
        return qs.order_by("-occurred_at", "-created_at")

    def create(self, request, *args, **kwargs):
        serializer = s.StockMovementCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        movements, created = record_stock_movement_idempotent(
            membership=self.membership, **serializer.validated_data
        )
        body = s.StockMovementResultSerializer(
            {"movements": movements}, context=self.get_serializer_context()
        )
        return Response(body.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


# --- Mantenimiento por condición (CBM-09): módulo `condition` del plan (API_MODULES) -----------


class ConditionViewSet(BaseViewSet):
    """Condición: exige `condition.view` (escribir pide `condition.manage` en el servicio)."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        require_perm(self.membership, Perm.CONDITION_VIEW)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param("include_inactive", "true para incluir las desactivadas.", OpenApiTypes.BOOL),
            _param("asset_class", "Reglas de esa clase de activo.", OpenApiTypes.UUID),
            _param("meter", "Reglas que aplican a esa variable (directas y por clase).", OpenApiTypes.UUID),
        ]
    ),
    create=extend_schema(
        request=s.ConditionRuleCreateSerializer,
        responses={201: s.ConditionRuleSerializer, 200: s.ConditionRuleSerializer},
        description="Crea una regla sobre una variable (`meter`) o sobre una clase de activo "
        "(`asset_class` + `meter_kind` + `meter_name`), con las validaciones de la web. "
        "Idempotente por `client_uuid`: reenviarlo devuelve la regla ya creada (200); usarlo en "
        "otra operación → 409.",
    ),
    partial_update=extend_schema(
        request=s.ConditionRuleUpdateSerializer,
        responses=s.ConditionRuleSerializer,
        description="Cambia la condición, la acción o los datos de la OT; `is_active: false` la "
        "desactiva (no se borra). El alcance no cambia: para otra variable, crea otra regla.",
    ),
)
class ConditionRuleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    ConditionViewSet,
):
    """Reglas de condición (CBM-03): por variable o por clase de activo. Sin borrado."""

    serializer_class = s.ConditionRuleSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        if self.action != "list":
            return rule_list(membership=self.membership, include_inactive=True)
        asset_class = meter = None
        if class_id := _uuid(self.request, "asset_class"):
            asset_class = AssetClass.objects.filter(pk=class_id).first()
            if asset_class is None:
                return ConditionRule.objects.none()
        if meter_id := _uuid(self.request, "meter"):
            meter = Meter.objects.filter(
                pk=meter_id, asset__in=visible_assets(membership=self.membership)
            ).first()
            if meter is None:
                return ConditionRule.objects.none()
        return rule_list(
            membership=self.membership,
            include_inactive=_flag(self.request, "include_inactive"),
            asset_class=asset_class,
            meter=meter,
        )

    def create(self, request, *args, **kwargs):
        serializer = s.ConditionRuleCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        rule, created = create_condition_rule_idempotent(
            membership=self.membership, **serializer.validated_data
        )
        rule = rule_for(membership=self.membership, pk=rule.pk)
        return Response(
            self.get_serializer(rule).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        rule = self.get_object()
        serializer = s.ConditionRuleUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        is_active = data.pop("is_active", None)
        if data:
            rule = condition_services.update_rule(membership=self.membership, rule=rule, **data)
        if is_active is not None:
            rule = condition_services.set_rule_active(
                membership=self.membership, rule=rule, is_active=is_active
            )
        return Response(self.get_serializer(rule_for(membership=self.membership, pk=rule.pk)).data)


ALERT_STATUSES = ["active", *[c for c, _l in ConditionAlert.Status.choices]]


@extend_schema_view(
    list=extend_schema(
        parameters=[
            _param(
                "status", "Estado; active = abiertas y reconocidas (por defecto, todas).", enum=ALERT_STATUSES
            ),
            _param("asset", "Activo.", OpenApiTypes.UUID),
        ]
    )
)
class ConditionAlertViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, ConditionViewSet):
    """Alertas de condición (CBM-05): abiertas primero y luego las más recientes."""

    serializer_class = s.ConditionAlertSerializer

    def get_queryset(self):
        if self.action != "list":
            return alert_list(membership=self.membership)
        asset = None
        if asset_id := _uuid(self.request, "asset"):
            asset = visible_assets(membership=self.membership).filter(pk=asset_id).first()
            if asset is None:
                return ConditionAlert.objects.none()
        return alert_list(
            membership=self.membership,
            status=self.request.query_params.get("status", "").strip(),
            asset=asset,
        )

    @extend_schema(request=None, responses=s.ConditionAlertSerializer)
    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        """Reconoce una alerta abierta (queda quién y cuándo); reconocerla otra vez no hace nada.

        Una alerta normalizada o cerrada no se reconoce → 400.
        """
        alert = self.get_object()
        condition_services.acknowledge_alert(membership=self.membership, alert=alert)
        return Response(self.get_serializer(alert_list(membership=self.membership).get(pk=alert.pk)).data)
