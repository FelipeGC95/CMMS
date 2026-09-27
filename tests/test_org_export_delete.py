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
