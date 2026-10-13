"""Serializadores de la API de terreno (`/field/api/`, X-06, ADR-011).

La forma de `bootstrap` y `snapshot` es el contrato con la PWA: el ejemplo completo
está en `apps/field/fixtures/demo.json` (los tests comparan las claves). Los decimales
van como texto; las horas en UTC (ISO 8601 con `Z`).
"""

import datetime
from decimal import Decimal

from rest_framework import serializers

from apps.work.models import LaborEntry, PartUsage, WorkOrder, WorkOrderChecklistItem
from apps.work.services import EXECUTION_TARGETS, allowed_transitions

from . import serializers as s


class UTCDateTimeField(serializers.DateTimeField):
    """Hora en UTC con `Z` (la v1 responde en la zona de la organización): el teléfono la
    muestra con `Intl` en la zona que trae `bootstrap`."""

    def __init__(self, **kwargs):
        super().__init__(default_timezone=datetime.UTC, **kwargs)


# --- bootstrap -------------------------------------------------------------------


class FieldPersonSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField(source="__str__")


class FieldOrganizationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    timezone = serializers.CharField()
    require_failure_details = serializers.BooleanField()


class FieldPermissionsSerializer(serializers.Serializer):
    execute = serializers.BooleanField(help_text="Ejecuta OT: checklist, horas, fotos, iniciar y completar.")
    request_create = serializers.BooleanField(help_text="Crea solicitudes (avisos de falla).")
    reading_create = serializers.BooleanField(help_text="Registra lecturas de medidor.")


# `from` es palabra reservada en Python: la clase se arma con `type`.
FieldTransitionRuleSerializer = type(
    "FieldTransitionRuleSerializer",
    (serializers.Serializer,),
    {
        "from": serializers.ListField(child=serializers.CharField(), help_text="Estados de origen."),
        "to": serializers.CharField(help_text="Estado de destino."),
    },
)


class FieldBootstrapSerializer(serializers.Serializer):
    user = FieldPersonSerializer()
    organization = FieldOrganizationSerializer()
    role = serializers.CharField()
    permissions = FieldPermissionsSerializer()
    horizon_days = serializers.IntegerField(help_text="Días hacia adelante del snapshot por defecto.")
    status_labels = serializers.DictField(
        child=serializers.CharField(),
        help_text="Nombre visible de cada estado de OT (la organización puede cambiarlos).",
    )
    execution_transitions = serializers.DictField(child=FieldTransitionRuleSerializer())
    server_time = UTCDateTimeField()
    min_client_version = serializers.IntegerField(
        help_text="Versión mínima del cliente; uno anterior recibe 409 `upgrade_required`."
    )


# --- snapshot --------------------------------------------------------------------


class FieldAssetRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()


class FieldLocationRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    path = serializers.CharField(
        source="__str__", help_text="Ruta completa, p. ej. «Planta › Sala eléctrica»."
    )


class FieldMeterRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    unit = serializers.CharField()


class FieldChecklistItemSerializer(serializers.ModelSerializer):
    position = serializers.IntegerField(source="order")
    answered_at = UTCDateTimeField(allow_null=True)
    photo_count = serializers.IntegerField(help_text="Fotos subidas al ítem.")
    variable = serializers.CharField(
        source="variable_label",
        help_text="Variable de condición que alimenta un ítem de medición (CBM-06); vacía si no alimenta.",
    )
    finding_code = serializers.SerializerMethodField(
        help_text="Código de la correctiva abierta creada desde este ítem (WO-06); null si no hay."
    )

    class Meta:
        model = WorkOrderChecklistItem
        fields = [
            "id",
            "position",
            "section",
            "text",
            "item_type",
            "unit",
            "min_value",
            "max_value",
            "required",
            "result",
            "value_number",
            "value_text",
            "note",
            "answered_at",
            "photo_count",
            "variable",
            "finding_code",
        ]
        read_only_fields = fields

    def get_finding_code(self, item) -> str | None:
        # Anotado por `field_work_orders` (sin consultas extra); fuera del snapshot no se calcula.
        number = getattr(item, "finding_number", None)
        if number is None:
            return None
        return WorkOrder(number=number, work_type=item.finding_type).code


class FieldPlannedPartSerializer(serializers.Serializer):
    """Repuesto previsto de la OT (reserva activa con pendiente, INV-10). Sin costos (Q3)."""

    id = serializers.UUIDField(help_text="Reserva.")
    part = serializers.UUIDField(source="part_id")
    code = serializers.CharField(source="part.code")
    name = serializers.CharField(source="part.name")
    unit = serializers.CharField(source="part.unit")
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3, help_text="Reservado.")
    consumed_quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    pending = serializers.DecimalField(
        max_digits=14, decimal_places=3, help_text="Lo que falta usar: la cantidad que propone «Usar»."
    )


class FieldPartUsageSerializer(serializers.ModelSerializer):
    """Repuesto o material ya registrado en la OT. Sin costos (Q3)."""

    class Meta:
        model = PartUsage
        fields = ["id", "part", "description", "quantity", "unit", "client_uuid", "created_at"]
        read_only_fields = fields


class FieldWorkOrderSerializer(serializers.ModelSerializer):
    """Una OT tal como se guarda en el teléfono (con su checklist incrustado)."""

    code = serializers.CharField()
    assigned_to_me = serializers.BooleanField(help_text="Responsable o apoyo es la persona.")
    assigned_to = FieldPersonSerializer(allow_null=True)
    asset = FieldAssetRefSerializer(allow_null=True)
    location = FieldLocationRefSerializer(allow_null=True)
    meter = FieldMeterRefSerializer(source="field_meter", allow_null=True)
    meter_required = serializers.BooleanField(help_text="Completar exige la lectura del medidor.")
    allowed_transitions = serializers.SerializerMethodField(
        help_text="Estados de ejecución a los que la persona puede llevar la OT."
    )
    checklist = FieldChecklistItemSerializer(source="field_checklist", many=True)
    planned_parts = FieldPlannedPartSerializer(
        source="field_planned_parts",
        many=True,
        help_text="Repuestos previstos con pendiente (vacío sin el módulo de inventario).",
    )
    parts = FieldPartUsageSerializer(source="field_parts", many=True, help_text="Repuestos ya registrados.")
    updated_at = UTCDateTimeField()

    class Meta:
        model = WorkOrder
        fields = [
            "id",
            "code",
            "title",
            "work_type",
            "status",
            "priority",
            "scheduled_for",
            "due_on",
            "estimated_minutes",
            "description",
            "assigned_to_me",
            "assigned_to",
            "asset",
            "location",
            "meter",
            "meter_required",
            "allowed_transitions",
            "updated_at",
            "checklist",
            "planned_parts",
            "parts",
        ]
        read_only_fields = fields

    def get_allowed_transitions(self, work_order) -> list[str]:
        targets = allowed_transitions(work_order=work_order, membership=self.context["membership"])
        return [status for status in targets if status in EXECUTION_TARGETS]


class FieldSnapshotSerializer(serializers.Serializer):
    generated_at = UTCDateTimeField()
    days = serializers.IntegerField()
    truncated = serializers.BooleanField(help_text="Había más OT que el tope (200).")
    work_orders = FieldWorkOrderSerializer(many=True)


# --- reference -------------------------------------------------------------------


class FieldFailureCodeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()


class FieldReferenceAssetSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()
    location_id = serializers.UUIDField(allow_null=True)
    location_code = serializers.CharField(source="location__code", allow_null=True)
    location_name = serializers.CharField(source="location__name", allow_null=True)


class FieldReferenceMeterSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    asset_id = serializers.UUIDField()
    kind = serializers.CharField()
    name = serializers.CharField()
    unit = serializers.CharField()
    is_cumulative = serializers.BooleanField()
    current_value = serializers.DecimalField(max_digits=14, decimal_places=3, allow_null=True)
    current_read_at = UTCDateTimeField(allow_null=True)


class FieldReferencePartSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()
    unit = serializers.CharField()
    category = serializers.CharField()


class FieldReferenceSerializer(serializers.Serializer):
    failure_codes = serializers.DictField(
        child=FieldFailureCodeSerializer(many=True), help_text="Códigos activos por tipo (modo, causa, …)."
    )
    assets = FieldReferenceAssetSerializer(many=True)
    truncated = serializers.BooleanField(help_text="Había más activos que el tope (2000).")
    meters = FieldReferenceMeterSerializer(many=True)
    parts = FieldReferencePartSerializer(
        many=True,
        help_text="Repuestos activos del catálogo, sin costos (vacío sin el módulo de inventario "
        "o sin permiso para verlo).",
    )
    parts_truncated = serializers.BooleanField(help_text="Había más repuestos que el tope (2000).")


# --- escrituras ------------------------------------------------------------------


class FieldTransitionSerializer(s.TransitionSerializer):
    """Sin conexión solo se ejecuta: iniciar o reanudar (`in_progress`), pausar (`on_hold`) o
    completar (`completed`; el cierre con horas y lectura va por `…/complete/`)."""

    to_status = serializers.ChoiceField([c for c in WorkOrder.Status.choices if c[0] in EXECUTION_TARGETS])


class FieldPartUsageCreateSerializer(s.PartUsageCreateSerializer):
    """Repuesto usado sin conexión (INV-13): del catálogo (`part`) o texto libre (`description`;
    sin `unit_cost` queda en 0 y quien gestiona la OT lo corrige en la web). Nunca hay
    conflicto por stock; solo 409 si la OT se canceló o cerró, o si el `client_uuid` ya se
    usó en otra OT."""

    free_text_default_cost = Decimal(0)


class LaborCreateSerializer(serializers.Serializer):
    minutes = serializers.IntegerField(min_value=1, max_value=24 * 60)
    client_uuid = serializers.UUIDField(required=False, allow_null=True)


class LaborEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = LaborEntry
        fields = ["id", "work_order", "minutes", "client_uuid", "created_at"]
        read_only_fields = fields


class FieldWorkRequestCreateSerializer(s.WorkRequestCreateSerializer):
    """Aviso de falla desde el teléfono: JSON o `multipart/form-data` con foto opcional."""

    photo = serializers.FileField(required=False, allow_null=True, help_text="Foto (JPEG, PNG, WebP o HEIC).")
    reported_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Hora del aviso sin conexión: inicio de la falla y de la detención. Vacía = ahora.",
    )


class FieldWorkRequestSerializer(s.WorkRequestSerializer):
    photo_warning = serializers.CharField(
        read_only=True, default="", help_text="Por qué el aviso se guardó sin la foto (cupo lleno)."
    )

    class Meta(s.WorkRequestSerializer.Meta):
        fields = [*s.WorkRequestSerializer.Meta.fields, "photo_warning"]
        read_only_fields = fields


# --- hallazgo (WO-06 CA2) ----------------------------------------------------------


class FieldFindingCreateSerializer(serializers.Serializer):
    """Aviso de hallazgo de un ítem No OK hecho sin conexión."""

    description = serializers.CharField(
        required=False, allow_blank=True, max_length=2000, default="", help_text="Detalle opcional."
    )
    asset_stopped = serializers.BooleanField(
        required=False, default=False, help_text="¿El equipo está detenido?"
    )
    client_uuid = serializers.UUIDField(required=False, allow_null=True)


class FieldFindingSerializer(serializers.Serializer):
    """Correctiva creada (o ya existente) desde el hallazgo; con la solicitud si pasó por el aviso."""

    id = serializers.UUIDField(help_text="OT correctiva.")
    code = serializers.CharField()
    status = serializers.CharField()
    item = serializers.UUIDField(source="source_checklist_item_id", help_text="Ítem de origen.")
    request_number = serializers.IntegerField(
        allow_null=True,
        help_text="N° de la solicitud cuando la creó el aviso de falla (técnico); si no, null.",
    )
