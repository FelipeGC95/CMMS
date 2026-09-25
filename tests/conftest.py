import datetime
from dataclasses import dataclass
from decimal import Decimal

import pytest

from apps.accounts.models import Membership, User
from apps.assets.models import Asset, AssetClass
from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType, MaintenancePlan
from apps.maintenance.services import create_plan
from apps.organizations.models import Organization

PASSWORD = "clave-de-prueba-123"


@dataclass
class Tenant:
    org: Organization
    admin: Membership
    tech: Membership
    viewer: Membership
    asset: Asset
    plan: MaintenancePlan


def make_tenant(slug: str) -> Tenant:
    org = Organization.objects.create(name=slug.title(), slug=slug)

    def member(role):
        user = User.objects.create_user(f"{role}@{slug}.cl", PASSWORD, full_name=f"{role} {slug}")
        return Membership.objects.create(user=user, organization=org, role=role)

    admin, tech, viewer = member("admin"), member("technician"), member("viewer")
    with tenant_context(org):
        cls = AssetClass.objects.create(code="CMP", name="Compresor")
        asset = Asset.objects.create(code="CMP-01", name="Compresor 1", asset_class=cls, criticality="A")
        plan = create_plan(
            code="PM1",
            name="Mensual",
            asset_class=cls,
            interval_days=30,
            items=[
                {"text": "Purgar", "item_type": ItemType.CHECK},
                {
                    "text": "Presión",
                    "item_type": ItemType.MEASUREMENT,
                    "unit": "bar",
                    "min_value": Decimal("6.5"),
                    "max_value": Decimal("8"),
                },
            ],
        )
    return Tenant(org, admin, tech, viewer, asset, plan)


@pytest.fixture
def tenant_a(db):
    return make_tenant("empresa-a")


@pytest.fixture
def tenant_b(db):
    return make_tenant("empresa-b")


@pytest.fixture
def in_a(tenant_a):
    with tenant_context(tenant_a.org):
        yield tenant_a


@pytest.fixture
def today():
    return datetime.date(2026, 9, 24)


def login(client, membership):
    client.force_login(membership.user)
    return client


def tiny_jpeg_bytes(size=(10, 10), color="red") -> bytes:
    """JPEG real y mínimo (no una firma falsa): Pillow debe poder abrirlo (AST-06)."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="JPEG")
    return buffer.getvalue()
