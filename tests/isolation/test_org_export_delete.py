"""Aislamiento de la exportación completa y la purga (X-03, X-04, NF-07).

La exportación de A nunca contiene filas ni archivos de B, y purgar A deja B
intacta. En PostgreSQL (CI) RLS es la segunda barrera: dentro del contexto de
A, ninguna tabla exportada devuelve filas de B aunque se consulte sin filtro.
"""

import pytest
from django.core.files.storage import default_storage
from django.db import connection
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.tenancy import platform_context, tenant_context
from apps.organizations import exports, purge, services
from apps.organizations.models import Organization
from tests.conftest import PASSWORD, login
from tests.org_data_utils import add_attachment, read_zip


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.DATA_EXPORT_USE_QUEUE = False


def _all_ids(tenant) -> set[str]:
    """Todos los id de filas de negocio de la organización (vía plataforma)."""
    ids = set()
    with platform_context():
        for model in exports.exportable_models():
            ids |= {
                str(pk)
                for pk in model._base_manager.filter(organization_id=tenant.org.pk).values_list(
                    "pk", flat=True
                )
            }
    return ids


def test_export_of_a_never_contains_b_rows_or_files(tenant_a, tenant_b, django_capture_on_commit_callbacks):
    add_attachment(tenant_a, name="manual-a.pdf", content=b"A")
    b_file = add_attachment(tenant_b, name="manual-b.pdf", content=b"SECRETO-B")
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True):
            export = services.request_data_export(membership=tenant_a.admin)
        export.refresh_from_db()
    files = read_zip(export.file)
    text = b"\n".join(data for name, data in files.items() if name.endswith((".csv", ".txt"))).decode(
        "utf-8-sig"
    )
    b_ids = _all_ids(tenant_b)
    assert b_ids  # B tiene datos: la prueba no es trivial
    assert str(tenant_b.org.pk) not in text
    assert not any(pk in text for pk in b_ids)
    assert "empresa-b.cl" not in text
    assert not any(name.endswith("manual-b.pdf") for name in files)
    assert b"SECRETO-B" not in b"".join(files.values())
    assert str(b_file.pk) not in "".join(files)


def test_admin_of_b_cannot_download_a_export(client, tenant_a, tenant_b, django_capture_on_commit_callbacks):
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True):
            export = services.request_data_export(membership=tenant_a.admin)
    login(client, tenant_b.admin)
    assert client.get(reverse("data_export_download", args=[export.pk])).status_code == 404
    body = client.get(reverse("organization_settings")).content.decode()
    assert str(export.pk) not in body


@pytest.mark.skipif(connection.vendor != "postgresql", reason="RLS solo existe en PostgreSQL")
def test_rls_hides_b_rows_from_every_exported_table(tenant_a, tenant_b):
    add_attachment(tenant_b)
    with tenant_context(tenant_a.org):
        for model in exports.exportable_models():
            # Sin filtro por organización: lo único que aísla es la política de la base.
            org_ids = set(model._base_manager.values_list("organization_id", flat=True))
            assert org_ids <= {tenant_a.org.pk}, model


def test_purging_a_leaves_b_intact(tenant_a, tenant_b):
    add_attachment(tenant_a)
    b_file = add_attachment(tenant_b)
    b_ids_before = _all_ids(tenant_b)
    b_members = set(Membership.objects.filter(organization=tenant_b.org).values_list("pk", flat=True))
    with tenant_context(tenant_a.org):
        org = services.request_organization_deletion(
            membership=tenant_a.admin, password=PASSWORD, confirm_name=tenant_a.org.name
        )

    purge.purge_organization(org)

    connection.check_constraints()
    assert _all_ids(tenant_a) == set()
    assert _all_ids(tenant_b) == b_ids_before
    assert set(Membership.objects.filter(organization=tenant_b.org).values_list("pk", flat=True)) == b_members
    assert default_storage.exists(b_file.file.name)
    tenant_b.org.refresh_from_db()
    assert tenant_b.org.status == Organization.Status.ACTIVE and tenant_b.org.is_active
    assert User.objects.filter(email="admin@empresa-b.cl", is_active=True).exists()


def test_b_admin_cannot_request_or_cancel_a_deletion(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        services.request_organization_deletion(
            membership=tenant_a.admin, password=PASSWORD, confirm_name=tenant_a.org.name
        )
    login(client, tenant_b.admin)
    assert "deletion-banner" not in client.get(reverse("dashboard")).content.decode()
    client.post(reverse("organization_delete_cancel"))  # actúa sobre B, que no tiene nada pendiente
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.status == Organization.Status.PENDING_DELETION
    client.post(reverse("organization_delete"), {"password": PASSWORD, "confirm_name": tenant_a.org.name})
    tenant_b.org.refresh_from_db()
    assert tenant_b.org.status == Organization.Status.ACTIVE  # el nombre de A no confirma B
