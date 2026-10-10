"""Endpoints de la API de terreno (`/field/api/`, X-06, ADR-011). Delgados: validan y llaman a
`api.field` (lecturas) o a los mismos servicios que la web y la API v1 (escrituras).

Autenticación por la sesión de la web (`FieldAPIMixin`): la PWA del técnico corre en el
mismo origen y nunca guarda un token.
"""

from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.assets.models import MeterReading
from apps.work import services as work_services
from apps.work.selectors import visible_work_orders

from . import field, v1
from . import field_serializers as fs
from . import serializers as s
from .framework import FieldAPIMixin, TenantAPIMixin
from .services import log_labor_idempotent

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
    """Códigos de falla activos, activos visibles (tope 2000 con `truncated`) y sus medidores."""

    def get(self, request):
        return Response(fs.FieldReferenceSerializer(field.reference(membership=self.membership)).data)


@extend_schema(tags=FIELD_TAG)
class FieldWorkOrderViewSet(FieldAPIMixin, v1.WorkOrderActionsMixin, v1.BaseViewSet):
    """Ejecución de una OT visible: estado, checklist, fotos, horas y cierre."""

    serializer_class = s.WorkOrderSerializer
    transition_serializer_class = fs.FieldTransitionSerializer

    def get_queryset(self):
        return visible_work_orders(membership=self.membership)

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
