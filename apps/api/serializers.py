"""Serializadores de la API v1: validan la entrada; las reglas viven en los servicios."""

from decimal import Decimal

from django.db.models import Q
from django.utils.translation import gettext as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.assets.models import (
    VARIABLE_KINDS,
    VARIABLE_METER_KIND_CHOICES,
    Asset,
    AssetClass,
    Meter,
    MeterReading,
)
from apps.assets.reading_imports import MAX_ROWS
from apps.assets.selectors import visible_assets
from apps.attachments.models import Attachment
from apps.condition.models import ConditionAlert, ConditionRule
from apps.inventory.models import Part, StockItem, StockMovement, Warehouse
from apps.inventory.selectors import warehouse_scope
from apps.organizations.models import Location
from apps.work.models import FailureEvent, PartUsage, WorkOrder, WorkOrderChecklistItem, WorkRequest


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


def _reading_meters(membership):
    # Las variables desactivadas también: el servicio responde que hay que reactivarlas (CBM-01 CA5).
    return Meter.objects.filter(
        Q(is_active=True) | Q(kind__in=VARIABLE_KINDS), asset__in=_assets(membership)
    ).select_related("asset")


class MeterReadingCreateSerializer(serializers.Serializer):
    meter = TenantPK(_reading_meters)
    value = serializers.DecimalField(
        max_digits=14,
        decimal_places=3,
        help_text="Negativo solo en variables de condición (p. ej. temperatura bajo cero).",
    )
    read_at = serializers.DateTimeField(required=False)
    is_reset = serializers.BooleanField(required=False, default=False)
    client_uuid = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, data):
        if data["value"] < 0 and not data["meter"].is_variable:  # CBM-01 CA2
            raise serializers.ValidationError({"value": [_("La lectura no puede ser negativa.")]})
        return data


class BulkReadingRowSerializer(serializers.Serializer):
    asset_code = serializers.CharField(required=False, allow_blank=True)
    meter = serializers.CharField(required=False, allow_blank=True)
    kind = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Con asset_code: tipo del medidor o variable (temperature, vibration, hours…).",
    )
    name = serializers.CharField(
        required=False, allow_blank=True, help_text="Con asset_code: nombre del medidor o variable."
    )
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
    client_uuid = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Reenviarlo devuelve el estado actual sin repetir el cambio.",
    )
    at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Hora real del cambio (cliente sin conexión); vacía = ahora.",
    )


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
    at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Hora real de término (cliente sin conexión); vacía = ahora.",
    )


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
            "meter_kind",
            "meter_name",
            "meter_reading",
        ]
        read_only_fields = fields


class ChecklistAnswerResultSerializer(ChecklistItemSerializer):
    superseded = serializers.BooleanField(
        read_only=True,
        default=False,
        help_text="La respuesta enviada era más antigua que la vigente (`answered_at`) y no se aplicó.",
    )

    class Meta(ChecklistItemSerializer.Meta):
        fields = [*ChecklistItemSerializer.Meta.fields, "superseded"]
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
    taken_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Hora de la foto (cliente sin conexión): con la OT ya completada se acepta "
        "si es anterior al término.",
    )


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
    answered_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Hora de la respuesta (cliente sin conexión); gana la última. Vacía = ahora.",
    )


# --- Inventario (INV-17) ---------------------------------------------------------------------

COST_HELP = "Solo con el permiso de costos de inventario (`inventory.cost_view`); si no, null."


def _cost_field(**kwargs):
    return serializers.DecimalField(read_only=True, allow_null=True, help_text=COST_HELP, **kwargs)


class HidesCostsMixin:
    """Deja en null los campos de costo si la membresía no ve costos de inventario (Q3).

    La vista pasa `show_costs` en el contexto; sin él, los costos no se muestran.
    """

    cost_fields: tuple = ()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("show_costs", False):
            for name in self.cost_fields:
                data[name] = None
        return data


def _parts(membership):
    return Part.objects.filter(is_active=True)


def _warehouses(membership):
    return Warehouse.objects.filter(warehouse_scope(membership), is_active=True)


class PartSerializer(HidesCostsMixin, serializers.ModelSerializer):
    cost_fields = ("avg_cost", "last_cost")
    avg_cost = _cost_field(max_digits=18, decimal_places=4)
    last_cost = _cost_field(max_digits=18, decimal_places=4)

    class Meta:
        model = Part
        fields = [
            "id",
            "code",
            "name",
            "description",
            "unit",
            "category",
            "manufacturer",
            "manufacturer_ref",
            "supplier",
            "notes",
            "is_active",
            "avg_cost",
            "last_cost",
            "last_received_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class PartWriteSerializer(serializers.Serializer):
    """Alta o edición de un repuesto (INV-01). El costo y el stock solo cambian con movimientos."""

    code = serializers.CharField(max_length=40)
    name = serializers.CharField(max_length=200)
    unit = serializers.CharField(max_length=20, required=False, allow_blank=True)
    category = serializers.CharField(max_length=80, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    manufacturer = serializers.CharField(max_length=120, required=False, allow_blank=True)
    manufacturer_ref = serializers.CharField(max_length=80, required=False, allow_blank=True)
    supplier = serializers.CharField(max_length=150, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(
        required=False, help_text="Solo al editar: false lo desactiva (sale de los buscadores)."
    )


class WarehouseSerializer(serializers.ModelSerializer):
    location_code = serializers.CharField(source="location.code", read_only=True, default=None)

    class Meta:
        model = Warehouse
        fields = ["id", "code", "name", "location", "location_code", "is_default", "is_active", "notes"]
        read_only_fields = fields


class StockItemSerializer(serializers.ModelSerializer):
    part_code = serializers.CharField(source="part.code", read_only=True)
    part_name = serializers.CharField(source="part.name", read_only=True)
    unit = serializers.CharField(source="part.unit", read_only=True)
    warehouse_code = serializers.CharField(source="warehouse.code", read_only=True)
    available = serializers.DecimalField(
        max_digits=15, decimal_places=3, read_only=True, help_text="Existencia − reservado."
    )
    below_min = serializers.BooleanField(
        source="is_below_min", read_only=True, help_text="Disponible menor que el mínimo (con mínimo > 0)."
    )

    class Meta:
        model = StockItem
        fields = [
            "id",
            "part",
            "part_code",
            "part_name",
            "unit",
            "warehouse",
            "warehouse_code",
            "on_hand",
            "reserved",
            "available",
            "min_quantity",
            "target_quantity",
            "bin_location",
            "below_min",
            "last_movement_at",
        ]
        read_only_fields = fields


class StockMovementSerializer(HidesCostsMixin, serializers.ModelSerializer):
    cost_fields = ("unit_cost", "total_cost", "avg_cost_after")
    part_code = serializers.CharField(source="part.code", read_only=True)
    warehouse_code = serializers.CharField(source="warehouse.code", read_only=True)
    unit_cost = _cost_field(max_digits=18, decimal_places=4)
    total_cost = _cost_field(max_digits=14, decimal_places=2)
    avg_cost_after = _cost_field(max_digits=18, decimal_places=4)

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "part",
            "part_code",
            "warehouse",
            "warehouse_code",
            "kind",
            "reason",
            "quantity",
            "unit_cost",
            "total_cost",
            "avg_cost_after",
            "on_hand_after",
            "work_order",
            "reverses",
            "transfer_group",
            "reference",
            "supplier",
            "note",
            "occurred_at",
            "client_uuid",
            "created_at",
        ]
        read_only_fields = fields


MOVEMENT_RECEIPT, MOVEMENT_COUNT, MOVEMENT_TRANSFER = "receipt", "count", "transfer"
MOVEMENT_KINDS = [
    (MOVEMENT_RECEIPT, "Entrada (INV-05)"),
    (MOVEMENT_COUNT, "Ajuste por conteo (INV-06)"),
    (MOVEMENT_TRANSFER, "Transferencia entre bodegas (INV-07)"),
]


class StockMovementCreateSerializer(serializers.Serializer):
    """Entrada (`receipt`), conteo (`count`) o transferencia (`transfer`); idempotente por `client_uuid`.

    - `receipt`: `quantity` > 0, `unit_cost` opcional (vacío = entra al promedio vigente),
      `warehouse` opcional (la por defecto), `reference`, `supplier`, `note`, `occurred_at`.
    - `count`: `counted_quantity` ≥ 0 en `warehouse`; `note` es el motivo (obligatorio si la
      diferencia es negativa o supera el 10 %). Sin diferencia no crea movimiento.
    - `transfer`: `quantity` > 0 de `warehouse` (origen) a `to_warehouse` (destino).
    """

    kind = serializers.ChoiceField(MOVEMENT_KINDS)
    part = TenantPK(_parts)
    quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, required=False, help_text="`receipt` y `transfer`."
    )
    counted_quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=0, required=False, help_text="`count`: cantidad contada."
    )
    warehouse = TenantPK(
        _warehouses,
        required=False,
        allow_null=True,
        help_text="Bodega (origen en `transfer`); vacía = la por defecto.",
    )
    to_warehouse = TenantPK(_warehouses, required=False, allow_null=True, help_text="`transfer`: destino.")
    unit_cost = serializers.DecimalField(
        max_digits=18, decimal_places=4, min_value=0, required=False, allow_null=True
    )
    reference = serializers.CharField(max_length=60, required=False, allow_blank=True, default="")
    supplier = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    occurred_at = serializers.DateTimeField(
        required=False, allow_null=True, help_text="Fecha real (vacía = ahora). No aplica a `transfer`."
    )
    client_uuid = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, data):
        kind = data["kind"]
        required = ["counted_quantity"] if kind == MOVEMENT_COUNT else ["quantity"]
        if kind == MOVEMENT_TRANSFER:
            required += ["warehouse", "to_warehouse"]
        missing = {name: [_("Este campo es obligatorio.")] for name in required if data.get(name) is None}
        if missing:
            raise serializers.ValidationError(missing)
        return data


class StockMovementResultSerializer(serializers.Serializer):
    movements = StockMovementSerializer(
        many=True,
        help_text="Movimientos creados (o ya creados con ese `client_uuid`): uno, dos en una "
        "transferencia (salida y entrada) o ninguno en un conteo sin diferencia.",
    )


class PartUsageSerializer(HidesCostsMixin, serializers.ModelSerializer):
    """Repuesto o material usado en la OT (WO-04 CA2, INV-09)."""

    cost_fields = ("unit_cost", "cost")
    unit_cost = _cost_field(max_digits=18, decimal_places=4)
    cost = _cost_field(max_digits=14, decimal_places=2)
    part_code = serializers.CharField(source="part.code", read_only=True, default=None)
    warehouse = serializers.UUIDField(
        source="stock_movement.warehouse_id",
        read_only=True,
        default=None,
        allow_null=True,
        help_text="Bodega de la que salió (solo repuestos del catálogo).",
    )

    class Meta:
        model = PartUsage
        fields = [
            "id",
            "work_order",
            "part",
            "part_code",
            "description",
            "quantity",
            "unit",
            "unit_cost",
            "cost",
            "warehouse",
            "stock_movement",
            "client_uuid",
            "created_at",
        ]
        read_only_fields = fields


class PartUsageCreateSerializer(serializers.Serializer):
    """Repuesto del catálogo (`part`) o texto libre (`description` y `unit_cost`).

    Con `part` el servidor fija descripción, unidad y costo (promedio vigente al consumir;
    un `unit_cost` enviado se ignora) y descuenta el stock y la reserva de la OT. Nunca se
    rechaza por falta de stock. La bodega sale sola salvo que se indique `warehouse`.
    """

    part = TenantPK(_parts, required=False, allow_null=True)
    description = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    unit = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    unit_cost = serializers.DecimalField(
        max_digits=18, decimal_places=4, min_value=0, required=False, allow_null=True
    )
    warehouse = TenantPK(_warehouses, required=False, allow_null=True)
    used_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text="Hora real del consumo (cliente sin conexión); vacía = ahora.",
    )
    client_uuid = serializers.UUIDField(required=False, allow_null=True)

    # Texto libre sin costo: la API v1 lo exige (como la web); la de terreno usa 0.
    free_text_default_cost = None

    def validate(self, data):
        if data.get("part"):
            data.pop("unit_cost", None)
            return data
        data.pop("part", None)
        data.pop("warehouse", None)
        if not data.get("description", "").strip():
            raise serializers.ValidationError(
                {"description": [_("Indica el repuesto del catálogo o su descripción.")]}
            )
        if data.get("unit_cost") is None:
            if self.free_text_default_cost is None:
                raise serializers.ValidationError({"unit_cost": [_("Indica el costo unitario.")]})
            data["unit_cost"] = self.free_text_default_cost
        return data


# --- Mantenimiento por condición (CBM-09): módulo `condition` del plan (API_MODULES) ----------


def _rule_meters(membership):
    # Cualquier medidor visible: el servicio responde si no es una variable (mensaje claro).
    return Meter.objects.filter(asset__in=visible_assets(membership=membership))


class ConditionRuleSerializer(serializers.ModelSerializer):
    label = serializers.CharField(read_only=True)
    meter_asset_code = serializers.CharField(source="meter.asset.code", read_only=True, default=None)
    asset_class_code = serializers.CharField(source="asset_class.code", read_only=True, default=None)

    class Meta:
        model = ConditionRule
        fields = [
            "id",
            "name",
            "label",
            "meter",
            "meter_asset_code",
            "asset_class",
            "asset_class_code",
            "meter_kind",
            "meter_name",
            "operator",
            "threshold",
            "min_value",
            "max_value",
            "consecutive",
            "action",
            "work_order_title",
            "work_order_priority",
            "work_order_instructions",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


def _rule_number(help_text):
    return serializers.DecimalField(
        max_digits=14, decimal_places=3, required=False, allow_null=True, help_text=help_text
    )


class ConditionRuleFieldsSerializer(serializers.Serializer):
    """Condición, acción y datos de la OT: lo que se puede editar de una regla (CBM-03)."""

    name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    operator = serializers.ChoiceField(ConditionRule.Operator.choices)
    threshold = _rule_number("Umbral de gt y lt, o cambio por día de rate (> 0).")
    min_value = _rule_number("Mínimo del rango (outside).")
    max_value = _rule_number("Máximo del rango (outside), mayor que el mínimo.")
    consecutive = serializers.IntegerField(
        required=False, min_value=1, max_value=100, help_text="Lecturas seguidas fuera de rango (1 a 100)."
    )
    action = serializers.ChoiceField(ConditionRule.Action.choices, required=False)
    work_order_title = serializers.CharField(max_length=200, required=False, allow_blank=True)
    work_order_priority = serializers.ChoiceField(
        WorkOrder.Priority.choices,  # mismos valores que ConditionRule.Priority
        required=False,
        allow_null=True,
        help_text="1 urgente … 4 baja; null: según la criticidad del activo.",
    )
    work_order_instructions = serializers.CharField(required=False, allow_blank=True)


class ConditionRuleCreateSerializer(ConditionRuleFieldsSerializer):
    """Una regla sobre una variable (`meter`) o sobre una clase de activo + `meter_kind` + `meter_name`."""

    meter = TenantPK(_rule_meters, required=False, allow_null=True)
    asset_class = TenantPK(lambda m: AssetClass.objects.all(), required=False, allow_null=True)
    meter_kind = serializers.ChoiceField(VARIABLE_METER_KIND_CHOICES, required=False, allow_blank=True)
    meter_name = serializers.CharField(max_length=80, required=False, allow_blank=True)
    client_uuid = serializers.UUIDField(required=False, allow_null=True)


class ConditionRuleUpdateSerializer(ConditionRuleFieldsSerializer):
    """Edición parcial: la condición, la acción, la OT o `is_active`. El alcance no cambia."""

    is_active = serializers.BooleanField(
        required=False, help_text="false la desactiva (sus alertas activas quedan normalizadas)."
    )

    def validate(self, data):
        scope = sorted(set(self.initial_data) & {"meter", "asset_class", "meter_kind", "meter_name"})
        if scope:
            raise serializers.ValidationError(
                {name: [_("El alcance de una regla no se cambia: crea otra regla.")] for name in scope}
            )
        return data


class ConditionAlertSerializer(serializers.ModelSerializer):
    rule_label = serializers.CharField(source="rule.label", read_only=True)
    meter_kind = serializers.CharField(source="meter.kind", read_only=True)
    meter_name = serializers.CharField(source="meter.name", read_only=True)
    unit = serializers.CharField(source="meter.unit", read_only=True)
    asset_code = serializers.CharField(source="asset.code", read_only=True)
    work_order_code = serializers.CharField(source="work_order.code", read_only=True, default=None)

    class Meta:
        model = ConditionAlert
        fields = [
            "id",
            "status",
            "rule",
            "rule_label",
            "meter",
            "meter_kind",
            "meter_name",
            "unit",
            "asset",
            "asset_code",
            "reading",
            "value",
            "opened_at",
            "last_value",
            "last_read_at",
            "work_order",
            "work_order_code",
            "acknowledged_at",
            "normalized_at",
            "closed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
