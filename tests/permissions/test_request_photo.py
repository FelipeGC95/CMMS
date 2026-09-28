"""Foto de una solicitud (aviso por QR, REQ-02): la ve quien ve la solicitud, por diseño.

`attachment_view` usa `visible_requests` para `target_type = work_request`: un técnico
que ve la solicitud ve su foto aunque no pueda decidirla en la bandeja (esa pantalla
exige gestionar OT). Fuera de su alcance por sitio, o desde otra organización, es 404.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Membership, MembershipLocation, User
from apps.attachments.models import Attachment
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.services import report_anonymous_failure
from tests.conftest import PASSWORD, login, tiny_jpeg_bytes

pytestmark = pytest.mark.django_db


@pytest.fixture
def request_photo(tenant_a, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    tenant_a.org.allow_anonymous_reports = True
    tenant_a.org.save()
    photo = SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
    with tenant_context(tenant_a.org):
        plant = Location.objects.create(code="P1", name="Planta 1")
        other = Location.objects.create(code="P2", name="Planta 2")
        tenant_a.asset.location = plant
        tenant_a.asset.save()
        report_anonymous_failure(asset=tenant_a.asset, name="Juan", description="Fuga", photo=photo)
        attachment = Attachment.objects.get()
    assert attachment.target_type == "work_request"
    return {"url": reverse("attachment", args=[attachment.pk]), "plant": plant, "other": other}


def _scoped(tenant, role, location, name):
    with tenant_context(tenant.org):
        user = User.objects.create_user(f"{name}@{tenant.org.slug}.cl", PASSWORD)
        member = Membership.objects.create(
            user=user, organization=tenant.org, role=role, restricted_to_locations=True
        )
        MembershipLocation.objects.create(membership=member, location=location)
    return member


def test_technician_who_sees_the_request_sees_its_photo(client, tenant_a, request_photo):
    login(client, tenant_a.tech)  # sin alcance por sitio: ve todas las solicitudes
    assert client.get(request_photo["url"]).status_code == 200
    login(client, _scoped(tenant_a, "technician", request_photo["plant"], "tec-p1"))
    assert client.get(request_photo["url"]).status_code == 200


@pytest.mark.parametrize("role", ["technician", "viewer", "supervisor"])
def test_member_outside_the_request_site_gets_404(client, tenant_a, request_photo, role):
    login(client, _scoped(tenant_a, role, request_photo["other"], f"{role}-p2"))
    assert client.get(request_photo["url"]).status_code == 404


def test_other_organization_gets_404(client, tenant_b, request_photo):
    login(client, tenant_b.admin)
    assert client.get(request_photo["url"]).status_code == 404
