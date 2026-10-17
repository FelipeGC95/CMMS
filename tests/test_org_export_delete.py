"""Exportación completa (X-03) y borrado de organización (X-04): servicios con base real."""

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.api.services import create_token
from apps.assets.models import Asset
from apps.audit.models import AuditEvent
from apps.core.tenancy import platform_context, tenant_context
from apps.notifications.models import Kind, Notification
from apps.organizations import exports, purge, services
from apps.organizations.models import DataExport, Organization
from tests.conftest import PASSWORD, login
from tests.org_data_utils import add_attachment, csv_rows, read_zip


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.DATA_EXPORT_USE_QUEUE = False


def _export(tenant, django_capture_on_commit_callbacks):
    with tenant_context(tenant.org):
        with django_capture_on_commit_callbacks(execute=True):
            export = services.request_data_export(membership=tenant.admin)
        export.refresh_from_db()
    return export


# ---------- Exportación ----------


def test_export_builds_zip_with_every_table_files_and_readme(tenant_a, django_capture_on_commit_callbacks):
    attachment = add_attachment(tenant_a)
    export = _export(tenant_a, django_capture_on_commit_callbacks)

    assert export.status == DataExport.Status.READY
    assert export.file.name.startswith(f"org/{tenant_a.org.pk}/exports/")
    assert export.size_bytes > 0
    assert export.expires_at - export.finished_at == datetime.timedelta(days=7)
    files = read_zip(export.file)
    assert "LEEME.txt" in files
    for model in exports.exportable_models():
        assert f"datos/{model._meta.app_label}_{model._meta.model_name}.csv" in files
    assert "datos/attachments_documentpage.csv" not in files
    assets = csv_rows(files["datos/assets_asset.csv"])
    assert [row["code"] for row in assets] == ["CMP-01"]
    assert {row["organization_id"] for row in assets} == {str(tenant_a.org.pk)}
    members = csv_rows(files["datos/membresias.csv"])
    assert {row["email"] for row in members} == {
        "admin@empresa-a.cl",
        "technician@empresa-a.cl",
        "viewer@empresa-a.cl",
    }
    manual = f"archivos/adjuntos/asset/manual/{attachment.pk}_manual.pdf"
    assert files[manual] == b"%PDF-1.4 manual"


def test_export_omits_secrets(tenant_a, django_capture_on_commit_callbacks):
    with tenant_context(tenant_a.org):
        create_token(membership=tenant_a.admin, name="ERP", scope="read")
    files = read_zip(_export(tenant_a, django_capture_on_commit_callbacks).file)
    rows = csv_rows(files["datos/api_apitoken.csv"])
    assert len(rows) == 1
    assert "token_hash" not in rows[0]


def test_export_notifies_requester_and_audits(tenant_a, django_capture_on_commit_callbacks):
    export = _export(tenant_a, django_capture_on_commit_callbacks)
    with tenant_context(tenant_a.org):
        notification = Notification.objects.get(kind=Kind.DATA_EXPORT_READY)
        assert notification.recipient_id == tenant_a.admin.user_id
        assert AuditEvent.objects.filter(action=AuditEvent.Action.CREATE, target_id=str(export.pk)).exists()


def test_only_one_export_in_progress(tenant_a):
    with tenant_context(tenant_a.org):
        services.request_data_export(membership=tenant_a.admin)  # sin ejecutar: queda pendiente
        with pytest.raises(ValidationError):
            services.request_data_export(membership=tenant_a.admin)


def test_download_is_audited_and_served(client, tenant_a, django_capture_on_commit_callbacks):
    export = _export(tenant_a, django_capture_on_commit_callbacks)
    login(client, tenant_a.admin)
    response = client.get(reverse("data_export_download", args=[export.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    with tenant_context(tenant_a.org):
        assert AuditEvent.objects.filter(action=AuditEvent.Action.DOWNLOAD, target_id=str(export.pk)).exists()


def test_expired_export_is_removed_and_no_longer_served(client, tenant_a, django_capture_on_commit_callbacks):
    export = _export(tenant_a, django_capture_on_commit_callbacks)
    name = export.file.name
    later = export.expires_at + datetime.timedelta(minutes=1)
    login(client, tenant_a.admin)
    assert purge.expire_all_data_exports(now=later) == 1
    with tenant_context(tenant_a.org):  # con RLS, sin contexto no se ve la fila
        export.refresh_from_db()
    assert export.status == DataExport.Status.EXPIRED
    assert not default_storage.exists(name)
    assert client.get(reverse("data_export_download", args=[export.pk])).status_code == 404


def test_settings_page_shows_export_and_delete_cards(client, tenant_a, django_capture_on_commit_callbacks):
    _export(tenant_a, django_capture_on_commit_callbacks)
    login(client, tenant_a.admin)
    body = client.get(reverse("organization_settings")).content.decode()
    assert 'id="export"' in body and "Descargar" in body
    assert reverse("organization_delete") in body


# ---------- Borrado ----------


def _request_deletion(tenant):
    with tenant_context(tenant.org):
        return services.request_organization_deletion(
            membership=tenant.admin, password=PASSWORD, confirm_name=tenant.org.name
        )


def test_deletion_request_requires_password_and_exact_name(tenant_a):
    with tenant_context(tenant_a.org):
        with pytest.raises(ValidationError):
            services.request_organization_deletion(
                membership=tenant_a.admin, password="mala", confirm_name=tenant_a.org.name
            )
        with pytest.raises(ValidationError):
            services.request_organization_deletion(
                membership=tenant_a.admin, password=PASSWORD, confirm_name="Otra"
            )
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.status == Organization.Status.ACTIVE


def test_deletion_request_schedules_grace_period_and_cancel_restores(tenant_a):
    org = _request_deletion(tenant_a)
    assert org.status == Organization.Status.PENDING_DELETION
    assert org.deletion_scheduled_for - org.deletion_requested_at == datetime.timedelta(days=30)
    assert org.deletion_requested_by == tenant_a.admin.user
    with tenant_context(tenant_a.org):
        org = services.cancel_organization_deletion(membership=tenant_a.admin)
        actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert org.status == Organization.Status.ACTIVE
    assert org.deletion_scheduled_for is None
    assert {AuditEvent.Action.DELETION_REQUEST, AuditEvent.Action.DELETION_CANCEL} <= actions


def test_banner_and_cancel_flow_via_views(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("organization_delete"), {"password": PASSWORD, "confirm_name": tenant_a.org.name}
    )
    assert response.status_code == 302
    body = client.get(reverse("dashboard")).content.decode()
    assert "deletion-banner" in body and reverse("organization_delete_cancel") in body
    client.post(reverse("organization_delete_cancel"))
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.status == Organization.Status.ACTIVE


def test_banner_hides_cancel_button_for_non_admins(client, tenant_a):
    _request_deletion(tenant_a)
    login(client, tenant_a.tech)
    body = client.get(reverse("dashboard")).content.decode()
    assert "deletion-banner" in body
    assert reverse("organization_delete_cancel") not in body


def test_purge_waits_for_the_grace_period(tenant_a):
    org = _request_deletion(tenant_a)
    assert purge.purge_due_organizations(now=org.deletion_scheduled_for - datetime.timedelta(hours=1)) == []
    assert purge.purge_due_organizations(now=org.deletion_scheduled_for) == [str(org.pk)]


def test_purge_removes_rows_files_memberships_and_leaves_tombstone(
    tenant_a, django_capture_on_commit_callbacks
):
    attachment = add_attachment(tenant_a)
    export = _export(tenant_a, django_capture_on_commit_callbacks)
    with tenant_context(tenant_a.org):
        create_token(membership=tenant_a.admin, name="ERP", scope="read")
    tenant_a.admin.user.last_organization = tenant_a.org
    tenant_a.admin.user.save(update_fields=["last_organization"])
    org = _request_deletion(tenant_a)
    files = [attachment.file.name, export.file.name]

    summary = purge.purge_organization(org)

    connection.check_constraints()  # las FK diferidas quedan consistentes
    with platform_context():
        for model in purge.tenant_models():
            assert not model._base_manager.filter(organization_id=org.pk).exists(), model
    assert not Membership.objects.filter(organization_id=org.pk).exists()
    assert not any(default_storage.exists(name) for name in files)
    assert not User.objects.filter(email__endswith="@api.galbust.invalid").exists()
    admin = User.objects.get(pk=tenant_a.admin.user_id)  # la cuenta sigue, sin datos
    assert admin.last_organization_id is None
    org.refresh_from_db()
    assert org.status == Organization.Status.DELETED and not org.is_active
    assert org.deleted_at is not None
    assert tenant_a.org.name not in (org.name, org.slug)
    assert org.purge_summary["rows"] == summary["rows"] > 0
    assert org.purge_summary["files"] == 2
    assert purge.purge_organization(org) == org.purge_summary  # idempotente


def test_purge_command_runs_due_purges(tenant_a):
    org = _request_deletion(tenant_a)
    Organization.objects.filter(pk=org.pk).update(deletion_scheduled_for=timezone.now())
    call_command("purge_organizations", "--dry-run")
    assert Organization.objects.get(pk=org.pk).status == Organization.Status.PENDING_DELETION
    call_command("purge_organizations")
    assert Organization.objects.get(pk=org.pk).status == Organization.Status.DELETED
    with platform_context():
        assert not Asset.all_tenants.filter(organization_id=org.pk).exists()


# ---------- Inventario (INV-14): exportación y purga ----------


def _inventory_data(tenant):
    """Una fila en cada tabla de inventario y los enlaces desde OT y planes (PROTECT entre apps)."""
    from apps.inventory import services as inventory
    from apps.maintenance.services import create_plan
    from apps.work.services import add_part_usage, create_work_order

    with tenant_context(tenant.org):
        part = inventory.create_part(membership=tenant.admin, code="ROD", name="Rodamiento")
        inventory.receive_stock(membership=tenant.admin, part=part, quantity=5, unit_cost="10")
        wo = create_work_order(work_type="corrective", title="x", asset=tenant.asset)
        add_part_usage(work_order=wo, membership=tenant.tech, part=part, quantity=2)
        inventory.reserve_for_work_order(work_order=wo, part=part, quantity=1, source="manual")
        create_plan(
            code="PMR",
            name="Con repuesto",
            calendar_interval=30,
            items=[{"text": "x", "item_type": "check"}],
            resources=[
                {"kind": "part", "description": "Rodamiento", "quantity": 1, "unit": "un", "part": part}
            ],
        )
    return part


def test_export_includes_inventory_tables(tenant_a, django_capture_on_commit_callbacks):
    part = _inventory_data(tenant_a)
    files = read_zip(_export(tenant_a, django_capture_on_commit_callbacks).file)
    movements = csv_rows(files["datos/inventory_stockmovement.csv"])
    assert sorted(row["kind"] for row in movements) == ["issue", "receipt"]
    for name in ("part", "warehouse", "stockitem", "reservation"):
        assert len(csv_rows(files[f"datos/inventory_{name}.csv"])) == 1, name
    # Los enlaces al catálogo desde la OT y los planes también se exportan.
    (usage,) = csv_rows(files["datos/work_partusage.csv"])
    assert usage["part_id"] == str(part.pk)
    assert usage["stock_movement_id"] == next(row["id"] for row in movements if row["kind"] == "issue")
    assert [row["part_id"] for row in csv_rows(files["datos/maintenance_planresource.csv"])] == [str(part.pk)]


def test_purge_removes_immutable_inventory_rows(tenant_a, tenant_b):
    from apps.inventory.models import Part, StockMovement

    _inventory_data(tenant_a)
    _inventory_data(tenant_b)
    org = _request_deletion(tenant_a)
    summary = purge.purge_organization(org)
    connection.check_constraints()
    assert summary["tables"]["inventory.stockmovement"] == 2
    for label in ("inventory.part", "inventory.warehouse", "inventory.stockitem", "inventory.reservation"):
        assert summary["tables"][label] == 1, label
    assert summary["tables"]["work.partusage"] == 1
    with platform_context():
        assert not StockMovement.all_tenants.filter(organization_id=org.pk).exists()
        assert StockMovement.all_tenants.filter(organization_id=tenant_b.org.pk).count() == 2
        assert Part.all_tenants.filter(organization_id=tenant_b.org.pk).count() == 1


# ---------- Mantenimiento por condición (CBM-10): exportación y purga ----------


def _condition_data(tenant):
    """Una regla, su estado, una alerta y su OT predictiva, y un ítem de checklist enlazado a
    la variable con su lectura (CBM-06: `meter_reading` es PROTECT entre apps)."""
    from decimal import Decimal

    from apps.condition import services as condition
    from apps.maintenance.models import ItemType
    from apps.work.models import WorkOrder, WorkOrderChecklistItem
    from apps.work.services import create_work_order, record_checklist_readings
    from tests.condition_utils import read, variable

    with tenant_context(tenant.org):
        meter = variable(tenant.asset)
        condition.create_rule(membership=tenant.admin, meter=meter, operator="gt", threshold="80")
        read(meter, "85")
        wo = create_work_order(work_type=WorkOrder.Type.INSPECTION, title="Termografía", asset=tenant.asset)
        WorkOrderChecklistItem.objects.create(
            work_order=wo,
            order=0,
            text="Temperatura rodamiento",
            item_type=ItemType.MEASUREMENT,
            unit="°C",
            meter_kind=meter.kind,
            meter_name=meter.name,
            value_number=Decimal("70"),
        )
        assert len(record_checklist_readings(work_order=wo)) == 1
    return meter


def test_export_includes_condition_tables(tenant_a, django_capture_on_commit_callbacks):
    meter = _condition_data(tenant_a)
    files = read_zip(_export(tenant_a, django_capture_on_commit_callbacks).file)
    for name in ("conditionrule", "conditionstate", "conditionalert"):
        rows = csv_rows(files[f"datos/condition_{name}.csv"])
        assert len(rows) == 1, name
        assert rows[0]["meter_id"] == str(meter.pk)
    (alert,) = csv_rows(files["datos/condition_conditionalert.csv"])
    assert alert["work_order_id"]
    # El ítem enlazado exporta su variable y la lectura que registró (CBM-06).
    (item,) = csv_rows(files["datos/work_workorderchecklistitem.csv"])
    assert (item["meter_kind"], item["meter_name"]) == (meter.kind, meter.name)
    readings = {row["id"] for row in csv_rows(files["datos/assets_meterreading.csv"])}
    assert item["meter_reading_id"] in readings


def test_purge_removes_condition_rows(tenant_a, tenant_b):
    from apps.condition.models import ConditionAlert, ConditionRule
    from apps.work.models import WorkOrderChecklistItem

    _condition_data(tenant_a)
    _condition_data(tenant_b)
    org = _request_deletion(tenant_a)
    summary = purge.purge_organization(org)
    connection.check_constraints()
    for label in ("condition.conditionrule", "condition.conditionstate", "condition.conditionalert"):
        assert summary["tables"][label] == 1, label
    # Ítem enlazado y su lectura (PROTECT): se borran juntos sin violar la FK.
    assert summary["tables"]["work.workorderchecklistitem"] == 1
    assert summary["tables"]["assets.meterreading"] == 2
    with platform_context():
        assert not ConditionAlert.all_tenants.filter(organization_id=org.pk).exists()
        assert ConditionRule.all_tenants.filter(organization_id=tenant_b.org.pk).count() == 1
        assert WorkOrderChecklistItem.all_tenants.filter(
            organization_id=tenant_b.org.pk, meter_reading__isnull=False
        ).exists()


def _assistant_data(tenant, settings, monkeypatch):
    """Una conversación del asistente con su pregunta y respuesta pendiente (ADR-018)."""
    from apps.assistant import services as assistant
    from tests.assistant_utils import install

    settings.ANTHROPIC_API_KEY = "test-key"
    install(monkeypatch)
    with tenant_context(tenant.org):
        return assistant.start_conversation(membership=tenant.tech, asset=tenant.asset, text="Ruido")


def test_export_and_purge_include_assistant_tables(
    tenant_a, tenant_b, settings, monkeypatch, django_capture_on_commit_callbacks
):
    from apps.assistant.models import AssistantConversation, AssistantMessage

    conversation = _assistant_data(tenant_a, settings, monkeypatch)
    _assistant_data(tenant_b, settings, monkeypatch)
    files = read_zip(_export(tenant_a, django_capture_on_commit_callbacks).file)
    (row,) = csv_rows(files["datos/assistant_assistantconversation.csv"])
    assert row["id"] == str(conversation.pk)
    assert len(csv_rows(files["datos/assistant_assistantmessage.csv"])) == 2

    org = _request_deletion(tenant_a)
    summary = purge.purge_organization(org)
    connection.check_constraints()
    assert summary["tables"]["assistant.assistantconversation"] == 1
    assert summary["tables"]["assistant.assistantmessage"] == 2
    with platform_context():
        assert not AssistantMessage.all_tenants.filter(organization_id=org.pk).exists()
        assert AssistantConversation.all_tenants.filter(organization_id=tenant_b.org.pk).count() == 1
