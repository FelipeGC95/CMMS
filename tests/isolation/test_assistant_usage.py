"""Costo del asistente (AI-05 CA2): reporte de plataforma que separa a cada organización.

Lee todas las organizaciones a propósito (`all_tenants`, solo superadmin), pero los tokens de
una nunca se suman a la fila de otra, y ningún usuario de una organización entra al reporte
(ver tests/permissions/test_assistant_usage.py).
"""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.assistant.selectors import usage_by_organization_month
from apps.core.tenancy import tenant_context
from tests.assistant_usage_utils import PRICES, usage


@pytest.fixture(autouse=True)
def prices(settings):
    settings.AI_MODEL_PRICES_USD = PRICES


def test_each_organization_row_only_sums_its_own_tokens(tenant_a, tenant_b):
    now = timezone.now()
    usage(tenant_a, at=now, output_tokens=1000)
    usage(tenant_b, at=now, output_tokens=7)

    rows = {row.organization_id: row for row in usage_by_organization_month(now=now)}

    assert rows[tenant_a.org.pk].output_tokens == 1000
    assert rows[tenant_b.org.pk].output_tokens == 7


def test_report_works_inside_a_tenant_context_and_still_shows_every_organization(tenant_a, tenant_b):
    # En el admin hay una organización activa en la petición: el reporte de plataforma igual ve
    # a todas (RLS desactivado solo para esas lecturas) y no deja el contexto cambiado.
    now = timezone.now()
    usage(tenant_a, at=now, output_tokens=10)
    usage(tenant_b, at=now - datetime.timedelta(minutes=1), output_tokens=20)
    with tenant_context(tenant_a.org):
        rows = usage_by_organization_month(now=now)
    assert {row.organization_id for row in rows} == {tenant_a.org.pk, tenant_b.org.pk}


def test_organization_b_admin_cannot_open_the_report(client, tenant_a, tenant_b):
    usage(tenant_a, at=timezone.now(), output_tokens=1000)
    user = tenant_b.admin.user
    user.is_staff = True
    user.save()
    client.force_login(user)
    response = client.get(reverse("admin:assistant_assistantusage_changelist"))
    assert response.status_code == 403
    assert tenant_a.org.name not in response.content.decode()
