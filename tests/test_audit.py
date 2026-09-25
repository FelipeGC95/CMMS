"""Auditoría (X-01): servicio, solo inserción, conexiones con activos y admin."""

import datetime

import pytest
from django.db import transaction
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Asset, AssetClass, Meter, MeterKind, MeterReading
from apps.assets.services import change_asset_status, record_meter_reading
from apps.audit.context import client_ip
from apps.audit.models import AppendOnlyError, AuditEvent
from apps.audit.services import diff, record, snapshot
from apps.core.tenancy import CrossTenantError, TenantNotSet, tenant_context
from tests.conftest import login

Action = AuditEvent.Action


def _events(target):
    return AuditEvent.objects.filter(target_id=str(target.pk))


# --- Servicio ------------------------------------------------------------------


def test_record_stores_actor_target_and_changes(in_a):
    event = record(
        action=Action.UPDATE, target=in_a.asset, membership=in_a.admin, changes={"name": ["A", "B"]}
    )
    event.refresh_from_db()
    assert event.organization_id == in_a.org.id
    assert event.actor == in_a.admin.user
    assert event.actor_type == AuditEvent.ActorType.USER
    assert event.actor_repr == in_a.admin.user.email
    assert event.target_repr == str(in_a.asset)
    assert event.changes == {"name": ["A", "B"]}


def test_record_without_actor_is_system(in_a):
    event = record(action=Action.CREATE, target=in_a.asset)
    assert event.actor is None
    assert event.actor_type == AuditEvent.ActorType.SYSTEM


def test_record_requires_tenant_context(tenant_a):
    with pytest.raises(TenantNotSet):
        record(action=Action.CREATE, target=tenant_a.asset)


def test_record_rejects_target_of_another_organization(in_a, tenant_b):
    with pytest.raises(CrossTenantError):
        record(action=Action.UPDATE, target=tenant_b.asset)


def test_record_never_stores_sensitive_fields(in_a):
    changes = {
        "name": ["A", "B"],
        "password": ["x", "y"],
        "api_token": ["x", "y"],
        "client_secret": ["x", "y"],
        "fields": {"qr_token": ["x", "y"], "code": ["1", "2"]},
    }
    event = record(action=Action.UPDATE, target=in_a.asset, changes=changes)
    event.refresh_from_db()
    assert event.changes == {"name": ["A", "B"], "fields": {"code": ["1", "2"]}}


def test_record_takes_origin_from_request(in_a):
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.7", HTTP_USER_AGENT="Firefox")
    request.user = in_a.admin.user
    event = record(action=Action.CREATE, target=in_a.asset, request=request)
    assert (event.ip, event.user_agent, event.actor) == ("10.0.0.7", "Firefox", in_a.admin.user)


def test_record_rolls_back_with_the_calling_transaction(in_a):
    with pytest.raises(RuntimeError), transaction.atomic():
        record(action=Action.CREATE, target=in_a.asset)
        raise RuntimeError
    assert not _events(in_a.asset).exists()


def test_diff_keeps_only_changed_fields():
    assert diff({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == {"b": [2, 3], "c": [None, 4]}


def test_snapshot_skips_system_fields(in_a):
    data = snapshot(in_a.asset)
    assert data["code"] == "CMP-01"
    assert data["asset_class_id"] == in_a.asset.asset_class_id
    assert not {"id", "organization_id", "created_at", "updated_at"} & data.keys()


# --- IP del cliente -----------------------------------------------------------


@pytest.mark.parametrize(
    ("trusted", "forwarded", "expected"),
    [
        (0, "1.1.1.1, 2.2.2.2", "9.9.9.9"),  # sin proxies: nunca X-Forwarded-For
        (1, "1.1.1.1, 2.2.2.2", "2.2.2.2"),  # el cliente pudo falsificar 1.1.1.1
        (2, "1.1.1.1, 2.2.2.2", "1.1.1.1"),
        (2, "2.2.2.2", "9.9.9.9"),  # menos saltos que proxies: REMOTE_ADDR
    ],
)
def test_client_ip_uses_trusted_proxies(trusted, forwarded, expected):
    meta = {"REMOTE_ADDR": "9.9.9.9", "HTTP_X_FORWARDED_FOR": forwarded}
    with override_settings(AUDIT_TRUSTED_PROXIES=trusted):
        assert client_ip(meta) == expected


# --- Solo inserción -----------------------------------------------------------


def test_events_cannot_be_updated_or_deleted(in_a):
    event = record(action=Action.CREATE, target=in_a.asset)
    event.target_repr = "otro"
    with pytest.raises(AppendOnlyError):
        event.save()
    with pytest.raises(AppendOnlyError):
        event.delete()
    with pytest.raises(AppendOnlyError):
        AuditEvent.objects.filter(pk=event.pk).update(target_repr="otro")
    with pytest.raises(AppendOnlyError):
        AuditEvent.objects.filter(pk=event.pk).delete()
    with pytest.raises(AppendOnlyError):
        AuditEvent.all_tenants.filter(pk=event.pk).update(target_repr="otro")
    with pytest.raises(AppendOnlyError):
        AuditEvent.all_tenants.all().delete()
    assert AuditEvent.objects.get(pk=event.pk).target_repr == str(in_a.asset)


# --- Activos ------------------------------------------------------------------


def test_asset_status_change_is_audited(in_a):
    user = in_a.admin.user
    change_asset_status(asset=in_a.asset, status=Asset.Status.STOPPED, by=user, reason="Falla")
    event = _events(in_a.asset).get()
    assert event.action == Action.TRANSITION
    assert event.actor == user
    assert event.changes == {"status": ["operational", "stopped"], "reason": "Falla"}


def test_unchanged_asset_status_is_not_audited(in_a):
    change_asset_status(asset=in_a.asset, status=in_a.asset.status)
    assert not _events(in_a.asset).exists()


def test_meter_reading_is_audited_once(in_a):
    meter = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    at = timezone.now() - datetime.timedelta(hours=1)
    key = "0b0d6c1e-1c2d-4c3e-9f00-000000000001"
    reading, _ = record_meter_reading(
        meter=meter, value=120, read_at=at, membership=in_a.tech, client_uuid=key
    )
    record_meter_reading(meter=meter, value=120, read_at=at, membership=in_a.tech, client_uuid=key)

    event = _events(reading).get()
    assert event.action == Action.CREATE
    assert event.actor == in_a.tech.user
    assert event.changes["value"] == "120"
    assert event.changes["source"] == MeterReading.Source.MANUAL
    assert MeterReading.objects.count() == 1


# --- Admin --------------------------------------------------------------------


@pytest.fixture
def admin_client(client, tenant_a):
    user = tenant_a.admin.user
    user.is_staff = user.is_superuser = True
    user.save()
    login(client, tenant_a.admin)
    return client


def test_admin_add_and_change_are_audited(admin_client, tenant_a):
    url = reverse("admin:assets_assetclass_add")
    response = admin_client.post(
        url, {"code": "BMB", "name": "Bomba", "is_active": "on"}, REMOTE_ADDR="10.1.1.1"
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        cls = AssetClass.objects.get(code="BMB")
        created = _events(cls).get()
    assert created.action == Action.ADMIN_CREATE
    assert created.actor == tenant_a.admin.user
    assert created.ip == "10.1.1.1"
    assert created.request_id
    assert created.changes["fields"]["name"] == "Bomba"

    url = reverse("admin:assets_assetclass_change", args=[cls.pk])
    admin_client.post(url, {"code": "BMB", "name": "Bomba centrífuga", "is_active": "on"})
    with tenant_context(tenant_a.org):
        changed = _events(cls).get(action=Action.ADMIN_UPDATE)
    assert changed.changes["fields"] == {"name": ["Bomba", "Bomba centrífuga"]}


def test_admin_bulk_delete_audits_every_object(admin_client, tenant_a):
    with tenant_context(tenant_a.org):
        classes = [AssetClass.objects.create(code=f"X{i}", name=f"Clase {i}") for i in range(3)]
    response = admin_client.post(
        reverse("admin:assets_assetclass_changelist"),
        {"action": "delete_selected", "_selected_action": [c.pk for c in classes], "post": "yes"},
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        assert not AssetClass.objects.filter(code__startswith="X").exists()
        deleted = AuditEvent.objects.filter(action=Action.ADMIN_DELETE)
        assert {e.target_id for e in deleted} == {str(c.pk) for c in classes}


def test_audit_admin_is_read_only(admin_client, tenant_a):
    with tenant_context(tenant_a.org):
        event = record(action=Action.CREATE, target=tenant_a.asset)
    assert admin_client.get(reverse("admin:audit_auditevent_changelist")).status_code == 200
    assert admin_client.get(reverse("admin:audit_auditevent_add")).status_code == 403
    assert admin_client.post(reverse("admin:audit_auditevent_delete", args=[event.pk])).status_code == 403
