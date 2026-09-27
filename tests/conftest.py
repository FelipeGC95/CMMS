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
            calendar_interval=30,
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


@pytest.fixture(autouse=True)
def _mfa_not_enforced(settings):
    """La obligación de MFA (ORG-08) se prueba en tests/test_mfa.py; el resto entra sin ella."""
    settings.MFA_ENFORCE = False


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


def pdf_bytes(pages: list[str]) -> bytes:
    """PDF real con una página por texto; una página vacía no tiene capa de texto (como un escaneo)."""
    import io

    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for text in pages:
        if text:
            pdf.drawString(72, 720, text)
        else:
            pdf.rect(72, 500, 300, 200, fill=1)  # solo dibujo, sin texto
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def make_manual(target, pages: list[str], *, by, name="manual.pdf"):
    """Sube un manual con el servicio y extrae su texto en el acto (en los tests no corre on_commit)."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.assets.models import AssetClass
    from apps.attachments import services
    from apps.attachments.models import TargetType

    target_type = TargetType.ASSET_CLASS if isinstance(target, AssetClass) else TargetType.ASSET
    manual = services.add_manual(
        target_type=target_type,
        target_id=target.pk,
        uploaded_file=SimpleUploadedFile(name, pdf_bytes(pages), content_type="application/pdf"),
        by=by,
    )
    services.extract_document_text(attachment=manual)
    return manual
