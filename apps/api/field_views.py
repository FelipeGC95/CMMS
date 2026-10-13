"""Endpoints de la API de terreno (`/field/api/`, X-06, ADR-011). Delgados: validan y llaman a
`api.field` (lecturas) o a los mismos servicios que la web y la API v1 (escrituras).

Autenticación por la sesión de la web (`FieldAPIMixin`): la PWA del técnico corre en el
mismo origen y nunca guarda un token.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.assets.models import MeterReading
from apps.work import services as work_services
from apps.work.models import WorkRequest
from apps.work.selectors import visible_work_orders

from . import field, v1
from . import field_serializers as fs
from . import serializers as s
from .framework import FieldAPIMixin, TenantAPIMixin
from .services import log_labor_idempotent, report_finding_idempotent

FIELD_TAG = ["field"]


class FieldView(FieldAPIMixin, TenantAPIMixin, APIView):
    pass


@extend_schema(tags=FIELD_TAG, responses=fs.FieldBootstrapSerializer)
class BootstrapView(FieldView):
    """Quién eres, tu organización, qué puedes hacer y la versión mínima del cliente."""

    check_client_version = False  # el cliente viejo debe poder leer `min_client_version`

    def get(self, request):
        return Response(fs.FieldBootstrapSerializer(field.bootstrap(membership=self.membership)).data)


@extend_schema(
    tags=FIELD_TAG,
    responses=fs.FieldSnapshotSerializer,
    parameters=[
        OpenApiParameter(
            "days", int, OpenApiParameter.QUERY, description="Días hacia adelante (7 por defecto, máx. 14)."
        )
    ],
)
class SnapshotView(FieldView):
    """OT visibles para trabajar sin conexión (asignadas primero; tope 200 con `truncated`)."""

    def get(self, request):
        days = field.parse_days(request.query_params.get("days"))
        data = field.snapshot(membership=self.membership, days=days)
        return Response(fs.FieldSnapshotSerializer(data, context={"membership": self.membership}).data)


@extend_schema(tags=FIELD_TAG, responses=fs.FieldReferenceSerializer)
class ReferenceView(FieldView):
    """Códigos de falla activos, activos visibles (tope 2000 con `truncated`), sus medidores y el
    catálogo de repuestos activos (tope 2000 con `parts_truncated`; sin costos)."""

    def get(self, request):
        return Response(fs.FieldReferenceSerializer(field.reference(membership=self.membership)).data)


@extend_schema(tags=FIELD_TAG)
class FieldWorkOrderViewSet(FieldAPIMixin, v1.WorkOrderActionsMixin, v1.BaseViewSet):
    """Ejecución de una OT visible: estado, checklist, fotos, horas y cierre."""

    serializer_class = s.WorkOrderSerializer
    transition_serializer_class = fs.FieldTransitionSerializer
    part_usage_serializer_class = fs.FieldPartUsageCreateSerializer
    allow_reopen = False  # una OT terminada por otra persona es un conflicto (409), no se reabre

    def get_queryset(self):
        return visible_work_orders(membership=self.membership)

    @extend_schema(
        methods=["GET"],
        responses=s.PartUsageSerializer(many=True),
        description="Repuestos y materiales usados en la OT (costos solo con `inventory.cost_view`).",
    )
    @extend_schema(
        methods=["POST"],
        request=fs.FieldPartUsageCreateSerializer,
        responses={201: s.PartUsageSerializer, 200: s.PartUsageSerializer},
        description="Repuesto usado sin conexión (INV-13): `part` del catálogo o `description`, "
        "con `quantity`, `client_uuid` y `used_at`. Sin conflictos de stock (puede quedar "
        "negativo). 409 `cancelled`/`locked` si la OT se canceló o cerró; reenviar el mismo "
        "`client_uuid` devuelve la línea (200); usarlo en otra OT → 409 `conflict`.",
    )
    @action(detail=True, methods=["get", "post"])
    def parts(self, request, pk=None):
        return super().parts(request, pk=pk)

    @extend_schema(request=fs.FieldTransitionSerializer, responses=s.WorkOrderSerializer)
    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """Iniciar, pausar o reanudar. Idempotente por `client_uuid`; `at` = hora real del cambio."""
        return super().transition(request, pk=pk)

    @extend_schema(
        request=fs.LaborCreateSerializer,
        responses={201: fs.LaborEntrySerializer, 200: fs.LaborEntrySerializer},
    )
    @action(detail=True, methods=["post"])
    def labor(self, request, pk=None):
        """Registra minutos trabajados por la persona (p. ej. un apoyo). Reenviar el mismo
        `client_uuid` devuelve el registro ya creado (200)."""
        work_order = self.get_object()
        serializer = fs.LaborCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry, created = log_labor_idempotent(
            membership=self.membership, work_order=work_order, **serializer.validated_data
        )
        return Response(
            fs.LaborEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        request=fs.FieldFindingCreateSerializer,
        responses={201: fs.FieldFindingSerializer, 200: fs.FieldFindingSerializer},
        parameters=[OpenApiParameter("item_id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
    )
    @action(detail=True, methods=["post"], url_path="checklist-items/<uuid:item_id>/finding")
    def finding(self, request, pk=None, item_id=None):
        """Avisa el hallazgo de un ítem No OK (WO-06 CA2) con el mismo servicio que la web: quien
        gestiona OT crea la correctiva; el técnico, la solicitud y su correctiva. Idempotente por
        ítem (201 la primera vez, 200 si ya tenía una correctiva abierta) y por `client_uuid`.
        Se acepta también con la OT completada o cerrada (el aviso pudo hacerse antes de
        completar); 409 `cancelled` si la OT se canceló; 400 si el ítem no está No OK."""
        return self._report_finding(request, item_id)

    @extend_schema(
        request=fs.FieldFindingCreateSerializer,
        responses={201: fs.FieldFindingSerializer, 200: fs.FieldFindingSerializer},
        parameters=[OpenApiParameter("item_id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
        deprecated=True,
    )
    @action(detail=True, methods=["post"], url_path="checklist/<uuid:item_id>/finding")
    def finding_legacy(self, request, pk=None, item_id=None):
        """Ruta anterior de `checklist-items/{item}/finding/` (la usaban los teléfonos con la
        versión de la PWA previa); se mantiene para que su cola llegue igual."""
        return self._report_finding(request, item_id)

    def _report_finding(self, request, item_id):
        item = self._checklist_item(item_id)
        serializer = fs.FieldFindingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        corrective, created = report_finding_idempotent(
            membership=self.membership, item=item, **serializer.validated_data
        )
        corrective.request_number = (
            WorkRequest.objects.filter(work_order=corrective).values_list("number", flat=True).first()
        )
        return Response(
            fs.FieldFindingSerializer(corrective).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=FIELD_TAG)
class FieldMeterViewSet(FieldAPIMixin, v1.MeterViewSet):
    """Medidores activos de los activos visibles."""


@extend_schema(tags=FIELD_TAG)
@extend_schema_view(
    create=extend_schema(request=s.MeterReadingCreateSerializer, responses={201: s.MeterReadingSerializer})
)
class FieldMeterReadingViewSet(FieldAPIMixin, v1.ReadingCreateMixin, v1.BaseViewSet):
    """Registra una lectura de medidor (AST-07); idempotente por `client_uuid`."""

    serializer_class = s.MeterReadingSerializer
    reading_source = MeterReading.Source.MANUAL


@extend_schema(tags=FIELD_TAG)
class FieldWorkRequestViewSet(FieldAPIMixin, v1.BaseViewSet):
    """Aviso de falla (REQ-01) con foto opcional; idempotente por `client_uuid`."""

    field_module = "requests"
    serializer_class = fs.FieldWorkRequestSerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    @extend_schema(
        request={
            "application/json": fs.FieldWorkRequestCreateSerializer,
            "multipart/form-data": fs.FieldWorkRequestCreateSerializer,
        },
        responses={201: fs.FieldWorkRequestSerializer},
    )
    def create(self, request, *args, **kwargs):
        serializer = fs.FieldWorkRequestCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        work_request = work_services.report_failure(membership=self.membership, **serializer.validated_data)
        return Response(fs.FieldWorkRequestSerializer(work_request).data, status=status.HTTP_201_CREATED)
