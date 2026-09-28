"""Serializadores de la API v1: validan la entrada; las reglas viven en los servicios."""

from django.utils.translation import gettext as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.assets.models import Asset, AssetClass, Meter, MeterReading
from apps.assets.reading_imports import MAX_ROWS
from apps.assets.selectors import visible_assets
from apps.attachments.models import Attachment
from apps.organizations.models import Location
from apps.work.models import FailureEvent, WorkOrder, WorkOrderChecklistItem, WorkRequest


@extend_schema_field(OpenApiTypes.UUID)
class TenantPK(serializers.PrimaryKeyRelatedField):
    """FK por id resuelta solo dentro de la organización (y el alcance) del token.

    Un id de otra organización responde igual que uno inexistente.
    """

    def __init__(self, get_queryset, **kwargs):
        self._get_queryset = get_queryset
        super().__init__(**kwargs)

    def get_queryset(self):
        return self._get_queryset(self.context["request"].auth)


def _assets(membership):
    return visible_assets(membership=membership).filter(is_active=True)


def _meters(membership):
    return Meter.objects.filter(is_active=True, asset__in=_assets(membership))


class AssetSerializer(serializers.ModelSerializer):
    asset_class_code = serializers.CharField(source="asset_class.code", read_only=True)
    location_code = serializers.CharField(source="location.code", read_only=True, default=None)

    class Meta:
        model = Asset
        fields = [
            "id",
            "code",
            "name",
            "asset_class",
            "asset_class_code",
            "location",
            "location_code",
            "parent",
            "status",
            "criticality",
            "manufacturer",
            "model",
            "serial_number",
            "year",
            "in_service_on",
            "attributes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class AssetWriteSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=30)
    name = serializers.CharField(max_length=150)
    asset_class = TenantPK(lambda m: AssetClass.objects.filter(is_active=True))
    location = TenantPK(lambda m: Location.objects.all(), required=False, allow_null=True)
    parent = TenantPK(_assets, required=False, allow_null=True)
    criticality = serializers.ChoiceField(Asset.Criticality.choices, required=False)
    manufacturer = serializers.CharField(max_length=100, required=False, allow_blank=True)
    model = serializers.CharField(max_length=100, required=False, allow_blank=True)
    serial_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    year = serializers.IntegerField(required=False, allow_null=True, min_value=1900, max_value=2100)
    in_service_on = serializers.DateField(required=False, allow_null=True)
    attributes = serializers.DictField(required=False)


class MeterSerializer(serializers.ModelSerializer):
    asset_code = serializers.CharField(source="asset.code", read_only=True)

    class Meta:
        model = Meter
        fields = [
            "id",
            "asset",
            "asset_code",
            "kind",
            "name",
            "unit",
            "is_cumulative",
            "current_value",
            "current_read_at",
            "avg_daily_usage",
        ]
        read_only_fields = fields


class MeterReadingSerializer(serializers.ModelSerializer):
    class Meta:
        model = MeterReading
        fields = ["id", "meter", "value", "read_at", "is_reset", "source", "work_order", "client_uuid"]
        read_only_fields = fields


class MeterReadingCreateSerializer(serializers.Serializer):
    meter = TenantPK(_meters)
    value = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=0)
    read_at = serializers.DateTimeField(required=False)
    is_reset = serializers.BooleanField(required=False, default=False)
    client_uuid = serializers.UUIDField(required=False, allow_null=True)


class BulkReadingRowSerializer(serializers.Serializer):
    asset_code = serializers.CharField(required=False, allow_blank=True)
    meter = serializers.CharField(required=False, allow_blank=True)
    value = serializers.CharField()
    read_at = serializers.CharField()
    is_reset = serializers.BooleanField(required=False, default=False)


class BulkReadingSerializer(serializers.Serializer):
    rows = BulkReadingRowSerializer(many=True, allow_empty=False, max_length=MAX_ROWS)


class BulkReadingRowResultSerializer(serializers.Serializer):
    row = serializers.IntegerField()
    meter = serializers.CharField(source="meter_id")
    asset_code = serializers.CharField()
    value = serializers.CharField()
    read_at = serializers.CharField()
    result = serializers.CharField(source="action")
    errors = serializers.ListField(child=serializers.CharField())


class BulkReadingResultSerializer(serializers.Serializer):
    created = serializers.IntegerField()
    duplicates = serializers.IntegerField()
    errors = serializers.IntegerField()
    rows = BulkReadingRowResultSerializer(many=True)


class WorkOrderSerializer(serializers.ModelSerializer):
    code = serializers.CharField(read_only=True)

    class Meta:
        model = WorkOrder
        fields = [
            "id",
            "code",
            "number",
            "work_type",
            "status",
            "priority",
            "title",
            "description",
            "asset",
            "location",
            "assigned_to",
            "due_on",
            "estimated_minutes",
            "started_at",
            "completed_at",
            "closed_at",
            "total_cost",
            "client_uuid",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class WorkOrderCreateSerializer(serializers.Serializer):
    work_type = serializers.ChoiceField(WorkOrder.Type.choices)
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    priority = serializers.ChoiceField(WorkOrder.Priority.choices, default=WorkOrder.Priority.NORMAL)
    asset = TenantPK(_assets, required=False, allow_null=True)
    location = TenantPK(lambda m: Location.objects.all(), required=False, allow_null=True)
    due_on = serializers.DateField(required=False, allow_null=True)
    estimated_minutes = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    client_uuid = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, data):
        if bool(data.get("asset")) == bool(data.get("location")):
            raise serializers.ValidationError(_("Indica un activo o una ubicación (solo uno)."))
        return data


class TransitionSerializer(serializers.Serializer):
    to_status = serializers.ChoiceField(WorkOrder.Status.choices)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class WorkRequestSerializer(serializers.ModelSerializer):
    work_order_code = serializers.CharField(source="work_order.code", read_only=True, default=None)

    class Meta:
        model = WorkRequest
        fields = [
            "id",
            "number",
            "asset",
            "location",
            "description",
            "asset_stopped",
            "status",
            "work_order",
            "work_order_code",
            "client_uuid",
            "created_at",
        ]
        read_only_fields = fields


class WorkRequestCreateSerializer(serializers.Serializer):
    description = serializers.CharField()
    asset = TenantPK(_assets, required=False, allow_null=True)
    location = TenantPK(lambda m: Location.objects.all(), required=False, allow_null=True)
    asset_stopped = serializers.BooleanField(required=False, default=False)
    client_uuid = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, data):
        if bool(data.get("asset")) == bool(data.get("location")):
            raise serializers.ValidationError(_("Indica un activo o una ubicación (solo uno)."))
        return data


class WorkOrderCompleteSerializer(serializers.Serializer):
    """Cierre de OT (WO-03, REQ-03). Las reglas son las del formulario web: lectura obligatoria
    si el plan usa medidor; en correctivas sobre un activo, impacto y (si la organización lo
    exige) modo y causa. Los campos de falla se ignoran en las demás OT."""

    labor_minutes = serializers.IntegerField(min_value=0, max_value=24 * 60)
    notes = serializers.CharField(
        required=False, allow_blank=True, max_length=4000, help_text="Comentario o acción realizada."
    )
    symptom = serializers.CharField(required=False, allow_blank=True, max_length=4000)
    impact = serializers.ChoiceField(FailureEvent.Impact.choices, required=False)
    failure_started_at = serializers.DateTimeField(required=False, allow_null=True)
    failure_ended_at = serializers.DateTimeField(required=False, allow_null=True)
    failure_mode = serializers.UUIDField(required=False, allow_null=True)
    failure_cause = serializers.UUIDField(required=False, allow_null=True)
    failure_mechanism = serializers.UUIDField(required=False, allow_null=True)
    detection_method = serializers.UUIDField(required=False, allow_null=True)
    component = serializers.UUIDField(required=False, allow_null=True)
    meter_value = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=0, required=False, allow_null=True
    )
    signature = serializers.CharField(
        required=False, allow_blank=True, help_text="Firma: data URL PNG en base64 (opcional)."
    )
    client_uuid = serializers.UUIDField(required=False, allow_null=True)


class ChecklistItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkOrderChecklistItem
        fields = [
            "id",
            "order",
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
        ]
        read_only_fields = fields


class ChecklistItemListSerializer(ChecklistItemSerializer):
    photo_count = serializers.IntegerField(read_only=True, help_text="Fotos subidas al ítem.")

    class Meta(ChecklistItemSerializer.Meta):
        fields = [*ChecklistItemSerializer.Meta.fields, "photo_count"]
        read_only_fields = fields


class AttachmentSerializer(serializers.ModelSerializer):
    """Metadatos de un adjunto. Nunca incluye una URL: el archivo se descarga desde la web,
    tras verificar permisos, con una URL firmada de corta duración (ADR-014)."""

    class Meta:
        model = Attachment
        fields = [
            "id",
            "target_type",
            "target_id",
            "purpose",
            "filename",
            "content_type",
            "size_bytes",
            "width",
            "height",
            "sha256",
            "client_uuid",
            "created_at",
        ]
        read_only_fields = fields


class PhotoUploadSerializer(serializers.Serializer):
    """Foto en `multipart/form-data` (JPEG, PNG, WebP o HEIC; hasta 20 MB). Se comprime a JPEG
    con el lado mayor ≤ 2000 px y sin metadatos (incluida la ubicación GPS)."""

    file = serializers.FileField()
    client_uuid = serializers.UUIDField(required=False, allow_null=True)


class ChecklistAnswerSerializer(serializers.Serializer):
    """Según el tipo del ítem: `result` (verificación), `value_number` (medición), `value_text`
    (texto) o `signature` (firma). La foto obligatoria se responde subiendo una foto."""

    result = serializers.ChoiceField(
        [
            WorkOrderChecklistItem.Result.OK,
            WorkOrderChecklistItem.Result.NOT_OK,
            WorkOrderChecklistItem.Result.NA,
        ],
        required=False,
    )
    value_number = serializers.DecimalField(max_digits=14, decimal_places=3, required=False, allow_null=True)
    value_text = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)
    signature = serializers.CharField(
        required=False, allow_blank=True, help_text="Firma: data URL PNG en base64."
    )
