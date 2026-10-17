"""Costo estimado del asistente por organización y mes (AI-05 CA2), con valores conocidos."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.assistant.selectors import message_cost_usd, usage_by_organization_month
from tests.assistant_usage_utils import NOW, PRICES, usage
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db

OCT = datetime.datetime(2026, 10, 3, 9, 0, tzinfo=datetime.UTC)
SEP = datetime.datetime(2026, 9, 20, 9, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def prices(settings):
    settings.AI_MODEL_PRICES_USD = PRICES


def test_message_cost_uses_each_token_price():
    # 1 M de entrada (4) + 0,1 M de salida (2) + 2 M leídos (0,4) + 0,4 M escritos (2) = 8,40 USD.
    cost = message_cost_usd(
        model="claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=2_000_000,
        cache_creation_tokens=400_000,
    )
    assert cost == Decimal("8.4")
    assert (
        message_cost_usd(
            model="otro-modelo", input_tokens=1, output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0
        )
        is None
    )


def test_usage_groups_by_organization_and_month(tenant_a, tenant_b):
    usage(
        tenant_a,
        at=OCT,
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=2_000_000,
        cache_creation_tokens=400_000,
    )
    usage(tenant_a, at=OCT + datetime.timedelta(days=5), model="claude-sonnet-5-5", input_tokens=500_000)
    usage(tenant_a, at=SEP, output_tokens=50_000)
    usage(tenant_b, at=OCT, cache_creation_tokens=200_000)
    usage(tenant_b, at=OCT, model="modelo-sin-precio", input_tokens=10)
    usage(tenant_a, at=datetime.datetime(2025, 10, 31, tzinfo=datetime.UTC), input_tokens=999)  # fuera
    usage(tenant_a, at=OCT, role="user", input_tokens=10**9)  # las preguntas no tienen tokens propios

    rows = usage_by_organization_month(now=NOW)

    by_key = {(row.organization, row.month): row for row in rows}
    assert len(rows) == 3
    october_a = by_key[(tenant_a.org.name, datetime.date(2026, 10, 1))]
    assert october_a.cost_usd == Decimal("9.4")  # 8,40 de Opus + 1,00 de Sonnet
    assert october_a.input_tokens == 1_500_000
    assert october_a.cache_read_tokens == 2_000_000
    assert october_a.conversations == 3  # también la de la pregunta
    assert october_a.replies == 2
    assert october_a.models == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert not october_a.unpriced
    assert by_key[(tenant_a.org.name, datetime.date(2026, 9, 1))].cost_usd == Decimal("1")
    october_b = by_key[(tenant_b.org.name, datetime.date(2026, 10, 1))]
    assert october_b.cost_usd == Decimal("1")  # 0,2 M escritos al caché x 5
    assert october_b.unpriced == ["modelo-sin-precio"]
    # El mes más reciente primero y, dentro del mes, de mayor a menor costo.
    assert [row.month.month for row in rows] == [10, 10, 9]
    assert rows[0].organization == tenant_a.org.name


def test_superuser_sees_cost_page(client, tenant_a):
    usage(tenant_a, at=timezone.now(), output_tokens=100_000)
    client.force_login(User.objects.create_superuser("root@galbust.cl", PASSWORD))

    response = client.get(reverse("admin:assistant_assistantusage_changelist"))

    assert response.status_code == 200
    assert [row.organization for row in response.context["rows"]] == [tenant_a.org.name]
    assert "Costo estimado (USD)" in response.content.decode()
    assert (
        reverse("admin:assistant_assistantusage_changelist")
        in client.get(reverse("admin:index")).content.decode()
    )
