"""Serializadores de la API de terreno (`/field/api/`, X-06, ADR-011).

La forma de `bootstrap` y `snapshot` es el contrato con la PWA: el ejemplo completo
está en `apps/field/fixtures/demo.json` (los tests comparan las claves). Los decimales
van como texto; las horas en UTC (ISO 8601 con `Z`).
"""

from rest_framework import serializers

from apps.work.models import LaborEntry, WorkOrder, WorkOrderChecklistItem
from apps.work.services import EXECUTION_TARGETS, allowed_transitions

from . import serializers as s

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
    server_time = serializers.DateTimeField()
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
    photo_count = serializers.IntegerField(help_text="Fotos subidas al ítem.")

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
        ]
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
        ]
        read_only_fields = fields

    def get_allowed_transitions(self, work_order) -> list[str]:
        targets = allowed_transitions(work_order=work_order, membership=self.context["membership"])
        return [status for status in targets if status in EXECUTION_TARGETS]


class FieldSnapshotSerializer(serializers.Serializer):
    generated_at = serializers.DateTimeField()
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
    current_read_at = serializers.DateTimeField(allow_null=True)


class FieldReferenceSerializer(serializers.Serializer):
    failure_codes = serializers.DictField(
        child=FieldFailureCodeSerializer(many=True), help_text="Códigos activos por tipo (modo, causa, …)."
    )
    assets = FieldReferenceAssetSerializer(many=True)
    truncated = serializers.BooleanField(help_text="Había más activos que el tope (2000).")
    meters = FieldReferenceMeterSerializer(many=True)


# --- escrituras ------------------------------------------------------------------


class FieldTransitionSerializer(s.TransitionSerializer):
    """Sin conexión solo se ejecuta: iniciar o reanudar (`in_progress`), pausar (`on_hold`) o
    completar (`completed`; el cierre con horas y lectura va por `…/complete/`)."""

    to_status = serializers.ChoiceField([c for c in WorkOrder.Status.choices if c[0] in EXECUTION_TARGETS])


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
