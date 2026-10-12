"""Aislamiento de la condición en la ficha del activo (C4, CBM-08 CA3): la organización A no ve,
crea, edita, desactiva ni lee variables, lecturas o tendencias de B, ni las referencia en el CSV."""

import datetime

from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import MembershipLocation
from apps.assets.models import Meter
from apps.assets.reading_imports import build_reading_preview
from apps.assets.services import record_meter_reading
from apps.condition import services
from apps.condition.selectors import asset_condition
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from tests.condition_utils import variable
from tests.conftest import login


def _variable_with_history(tenant, name):
    with tenant_context(tenant.org):
        meter = variable(tenant.asset, name=name)
        services.create_rule(membership=tenant.admin, meter=meter, operator="gt", threshold="80")
        record_meter_reading(meter=meter, value="85", read_at=timezone.now() - datetime.timedelta(days=1))
        return meter


def test_asset_detail_shows_only_own_variables_and_trend(client, tenant_a, tenant_b):
    _variable_with_history(tenant_a, "Propia")
    secret = _variable_with_history(tenant_b, "Secreta de B")
    login(client, tenant_a.admin)
    body = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert "Propia" in body
    assert "Secreta de B" not in body and str(secret.pk) not in body
    assert client.get(reverse("asset_detail", args=[tenant_b.asset.pk])).status_code == 404


def test_selector_does_not_read_variables_of_another_org(tenant_a, tenant_b):
    _variable_with_history(tenant_b, "Secreta de B")
    with tenant_context(tenant_a.org):
        assert asset_condition(membership=tenant_a.admin, asset=tenant_b.asset) == []


def test_variable_screens_of_another_org_are_404(client, tenant_a, tenant_b):
    meter_b = _variable_with_history(tenant_b, "Secreta de B")
    login(client, tenant_a.admin)
    assert client.get(reverse("asset_variable_create", args=[tenant_b.asset.pk])).status_code == 404
    response = client.post(
        reverse("asset_variable_create", args=[tenant_b.asset.pk]), {"kind": "temperature"}
    )
    assert response.status_code == 404
    for asset_pk in (tenant_a.asset.pk, tenant_b.asset.pk):
        edit = reverse("asset_variable_edit", args=[asset_pk, meter_b.pk])
        assert client.get(edit).status_code == 404
        assert client.post(edit, {"name": "x"}).status_code == 404
        active = reverse("asset_variable_active", args=[asset_pk, meter_b.pk])
        assert client.post(active, {"is_active": "0"}).status_code == 404
        reading = reverse("meter_reading", args=[asset_pk, meter_b.pk])
        assert client.post(reading, {f"{meter_b.pk}-value": "-5"}).status_code == 404
    with tenant_context(tenant_b.org):
        meter_b.refresh_from_db()
        assert (meter_b.name, meter_b.is_active, meter_b.readings.count()) == ("Secreta de B", True, 1)
    with tenant_context(tenant_a.org):
        assert not Meter.objects.exists()


def test_csv_does_not_resolve_variables_of_another_org(tenant_a, tenant_b):
    meter_b = _variable_with_history(tenant_b, "Rodamiento")
    with tenant_context(tenant_a.org):
        rows = [
            {"meter": str(meter_b.pk), "value": "1", "read_at": "2026-09-20", "_row": 2},
            {
                "asset_code": "CMP-01",
                "kind": "temperatura",
                "name": "Rodamiento",
                "value": "1",
                "read_at": "2026-09-20",
                "_row": 3,
            },
        ]
        preview = build_reading_preview(rows, membership=tenant_a.admin)
        assert "Medidor no encontrado" in preview.rows[0].errors[0]
        assert preview.rows[1].errors  # CMP-01 de A no tiene esa variable
        assert not preview.valid_rows


def test_site_scope_hides_variables_outside_the_membership_sites(client, tenant_a):
    meter = _variable_with_history(tenant_a, "Rodamiento")
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        tenant_a.asset.location = south
        tenant_a.asset.save(update_fields=["location"])
        MembershipLocation.objects.create(membership=tenant_a.admin, location=north)
        tenant_a.admin.restricted_to_locations = True
        tenant_a.admin.save(update_fields=["restricted_to_locations"])
    login(client, tenant_a.admin)
    assert client.get(reverse("asset_variable_create", args=[tenant_a.asset.pk])).status_code == 404
    edit = reverse("asset_variable_edit", args=[tenant_a.asset.pk, meter.pk])
    assert client.get(edit).status_code == 404
    with tenant_context(tenant_a.org):
        assert (
            build_reading_preview(
                [{"asset_code": "CMP-01", "kind": "temperature", "value": "1", "read_at": "2026-09-20"}],
                membership=tenant_a.admin,
            )
            .rows[0]
            .errors
        )
