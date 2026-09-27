"""REQ-02: aviso por QR sin cuenta (servicio, vista pública y anti-abuso)."""

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.assets.models import Asset
from apps.attachments.models import Attachment, TargetType
from apps.core.tenancy import tenant_context
from apps.notifications.models import Kind, Notification
from apps.work.models import FailureEvent, WorkOrder, WorkRequest
from apps.work.services import accept_request, reject_request, report_anonymous_failure
from tests.conftest import login, tiny_jpeg_bytes
from tests.test_wo_checklist import _fill_quota


@pytest.fixture(autouse=True)
def _fast_form(settings, db):
    settings.QR_REPORT_MIN_SECONDS = 0
    cache.clear()


def enable(tenant):
    tenant.org.allow_anonymous_reports = True
    tenant.org.save()


def qr_url(tenant):
    return reverse("asset_qr", args=[tenant.asset.qr_token])


def form_data(client, tenant, **overrides):
    form = client.get(qr_url(tenant)).context["form"]
    data = {
        "name": "Juan Operador",
        "description": "Pierde aceite",
        "answer": form._challenge["a"] + form._challenge["b"],
        "challenge": form.initial["challenge"],
        "website": "",
    }
    data.update(overrides)
    return data


def photo():
    return SimpleUploadedFile("foto.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")


def requests_of(tenant):
    with tenant_context(tenant.org):
        return list(WorkRequest.objects.all())


# --- Servicio -----------------------------------------------------------------------


def test_anonymous_report_needs_review_and_never_touches_the_asset(in_a, django_capture_on_commit_callbacks):
    enable(in_a)
    with django_capture_on_commit_callbacks(execute=True):
        request = report_anonymous_failure(
            asset=in_a.asset, name=" Juan ", description="Se detuvo", asset_stopped=True
        )
    assert request.is_anonymous and request.anonymous_name == "Juan"
    assert request.requested_by is None and request.work_order is None
    assert request.status == WorkRequest.Status.PENDING and request.asset_stopped
    assert not WorkOrder.objects.exists() and not FailureEvent.objects.exists()
    assert Asset.objects.get(pk=in_a.asset.pk).status == Asset.Status.OPERATIONAL
    # El aviso de "nueva solicitud" llega a quien decide (admin), no a técnicos ni lectores.
    assert Notification.objects.filter(recipient=in_a.admin.user, kind=Kind.REQUEST_NEW).exists()
    assert not Notification.objects.filter(recipient=in_a.tech.user).exists()


def test_anonymous_report_is_refused_when_the_organization_does_not_allow_it(in_a):
    from apps.accounts.permissions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        report_anonymous_failure(asset=in_a.asset, name="Juan", description="x")
    assert not WorkRequest.objects.exists()


def test_accepting_an_anonymous_report_creates_the_work_order_and_moves_the_photo(in_a, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    enable(in_a)
    request = report_anonymous_failure(
        asset=in_a.asset, name="Juan", description="Ruido fuerte", asset_stopped=True, photo=photo()
    )
    attachment = Attachment.objects.get()
    assert (attachment.target_type, attachment.target_id) == (TargetType.WORK_REQUEST, request.pk)
    assert attachment.uploaded_by is None
    accept_request(work_request=request, membership=in_a.admin)
    request.refresh_from_db()
    work_order = request.work_order
    assert request.status == WorkRequest.Status.ACCEPTED and work_order is not None
    assert (
        work_order.work_type == WorkOrder.Type.CORRECTIVE and work_order.status == WorkOrder.Status.REQUESTED
    )
    assert work_order.priority == WorkOrder.Priority.URGENT  # criticidad A y "detenido"
    assert "Juan" in work_order.description
    attachment.refresh_from_db()
    assert (attachment.target_type, attachment.target_id) == (TargetType.WORK_ORDER, work_order.pk)
    # Aceptar no cambia el estado del activo: "detenido" fue información sin verificar.
    assert Asset.objects.get(pk=in_a.asset.pk).status == Asset.Status.OPERATIONAL


def test_rejecting_an_anonymous_report_needs_no_work_order(in_a):
    enable(in_a)
    request = report_anonymous_failure(asset=in_a.asset, name="Juan", description="x")
    reject_request(work_request=request, membership=in_a.admin, reason="Duplicado")
    request.refresh_from_db()
    assert request.status == WorkRequest.Status.REJECTED and request.work_order is None


def test_anonymous_report_without_room_is_saved_without_photo(tenant_a, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    enable(tenant_a)
    _fill_quota(tenant_a)
    with tenant_context(tenant_a.org):
        request = report_anonymous_failure(asset=tenant_a.asset, name="Juan", description="x", photo=photo())
        assert request.photo_warning
        assert not Attachment.objects.filter(target_id=request.pk).exists()
        assert WorkRequest.objects.filter(pk=request.pk).exists()


# --- Vista pública ------------------------------------------------------------------


def test_public_form_when_enabled_shows_only_the_asset_and_organization(client, tenant_a):
    enable(tenant_a)
    response = client.get(qr_url(tenant_a))
    assert response.status_code == 200
    html = response.content.decode()
    assert tenant_a.asset.code in html and tenant_a.asset.name in html and tenant_a.org.name in html
    assert "navbar" not in html and "bottom-nav" not in html
    assert "no-store" in response["Cache-Control"]
    assert "Content-Security-Policy" in response


def test_public_submission_creates_an_anonymous_request(client, tenant_a, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    enable(tenant_a)
    data = form_data(client, tenant_a, asset_stopped="on", photo=photo())
    response = client.post(qr_url(tenant_a), data)
    assert response.status_code == 302
    assert response["Location"] == reverse("asset_qr_sent", args=[tenant_a.asset.qr_token])
    (request,) = requests_of(tenant_a)
    assert request.is_anonymous and request.asset_stopped and request.anonymous_name == "Juan Operador"
    with tenant_context(tenant_a.org):
        assert Attachment.objects.filter(target_id=request.pk).exists()
        assert not WorkOrder.objects.exists()
    assert client.get(response["Location"]).status_code == 200


def test_disabled_organization_keeps_the_login_redirect(client, tenant_a):
    response = client.get(qr_url(tenant_a))
    assert response.status_code == 302 and reverse("account_login") in response["Location"]
    unknown = client.get(reverse("asset_qr", args=["no-existe"]))
    assert unknown.status_code == 302 and reverse("account_login") in unknown["Location"]


def test_with_session_the_qr_still_opens_the_authenticated_report(client, tenant_a):
    enable(tenant_a)
    login(client, tenant_a.viewer)
    response = client.get(qr_url(tenant_a))
    assert response.status_code == 302
    assert response["Location"] == reverse("asset_report", args=[tenant_a.asset.pk])


def test_plan_without_requests_module_hides_the_public_form(client, tenant_a):
    from apps.organizations.models import Plan

    enable(tenant_a)
    tenant_a.org.plan = Plan.objects.create(code="solo-activos", name="Activos", features=["assets"])
    tenant_a.org.save()
    response = client.get(qr_url(tenant_a))
    assert response.status_code == 302 and reverse("account_login") in response["Location"]


def test_inbox_flags_anonymous_requests(client, tenant_a):
    enable(tenant_a)
    with tenant_context(tenant_a.org):
        report_anonymous_failure(asset=tenant_a.asset, name="Juan", description="Humo", asset_stopped=True)
    login(client, tenant_a.admin)
    html = client.get(reverse("request_list")).content.decode()
    assert "Anónima — requiere revisión" in html and "Reporta equipo detenido" in html and "Juan" in html


def test_inbox_photo_of_an_anonymous_request(client, tenant_a, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    enable(tenant_a)
    with tenant_context(tenant_a.org):
        request = report_anonymous_failure(asset=tenant_a.asset, name="Juan", description="x", photo=photo())
    login(client, tenant_a.admin)
    assert reverse("request_photo", args=[request.pk]) in client.get(reverse("request_list")).content.decode()
    assert client.get(reverse("request_photo", args=[request.pk])).status_code == 200


def test_accept_from_the_inbox_creates_the_work_order(client, tenant_a):
    enable(tenant_a)
    with tenant_context(tenant_a.org):
        request = report_anonymous_failure(asset=tenant_a.asset, name="Juan", description="x")
    login(client, tenant_a.admin)
    client.post(reverse("request_accept", args=[request.pk]))
    with tenant_context(tenant_a.org):
        request.refresh_from_db()
        assert request.status == WorkRequest.Status.ACCEPTED and request.work_order_id


# --- Anti-abuso (CA2) ------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"website": "http://spam"}, {"answer": 999}, {"challenge": "manipulado"}, {"challenge": ""}],
    ids=["honeypot", "wrong-sum", "bad-token", "missing-token"],
)
def test_bots_are_rejected(client, tenant_a, overrides):
    enable(tenant_a)
    response = client.post(qr_url(tenant_a), form_data(client, tenant_a, **overrides))
    assert response.status_code == 200
    assert not requests_of(tenant_a)


def test_forged_token_is_rejected_even_if_answer_matches_the_new_challenge(client, tenant_a, monkeypatch):
    enable(tenant_a)
    data = form_data(client, tenant_a, challenge="manipulado", answer=6)
    monkeypatch.setattr("apps.accounts.antibot.secrets.randbelow", lambda n: 2)  # nuevo desafío: 3 + 3
    assert client.post(qr_url(tenant_a), data).status_code == 200
    assert not requests_of(tenant_a)


def test_signup_token_is_not_valid_on_the_public_form(client, tenant_a):
    enable(tenant_a)
    signup_form = client.get(reverse("signup")).context["form"]
    data = form_data(
        client,
        tenant_a,
        challenge=signup_form.initial["challenge"],
        answer=signup_form._challenge["a"] + signup_form._challenge["b"],
    )
    assert client.post(qr_url(tenant_a), data).status_code == 200
    assert not requests_of(tenant_a)


def test_too_fast_submission_is_rejected(client, tenant_a, settings):
    enable(tenant_a)
    settings.QR_REPORT_MIN_SECONDS = 60
    assert client.post(qr_url(tenant_a), form_data(client, tenant_a)).status_code == 200
    assert not requests_of(tenant_a)


def test_rate_limit_per_ip(client, tenant_a, settings):
    enable(tenant_a)
    settings.QR_REPORT_LIMIT_PER_IP = (2, 3600)
    for _ in range(2):
        assert client.post(qr_url(tenant_a), form_data(client, tenant_a)).status_code == 302
    response = client.post(qr_url(tenant_a), form_data(client, tenant_a))
    assert response.status_code == 429
    assert len(requests_of(tenant_a)) == 2


def test_rate_limit_per_ip_counts_failed_attempts(client, tenant_a, settings):
    enable(tenant_a)
    settings.QR_REPORT_LIMIT_PER_IP = (2, 3600)
    for _ in range(2):
        client.post(qr_url(tenant_a), form_data(client, tenant_a, answer=999))
    assert client.post(qr_url(tenant_a), form_data(client, tenant_a)).status_code == 429
    assert not requests_of(tenant_a)


def test_rate_limit_per_asset_across_ips(client, tenant_a, settings):
    enable(tenant_a)
    settings.QR_REPORT_LIMIT_PER_ASSET = (2, 86400)
    for i in range(2):
        data = form_data(client, tenant_a)
        assert client.post(qr_url(tenant_a), data, REMOTE_ADDR=f"10.0.0.{i}").status_code == 302
    response = client.post(qr_url(tenant_a), form_data(client, tenant_a), REMOTE_ADDR="10.0.0.9")
    assert response.status_code == 429
    assert len(requests_of(tenant_a)) == 2


def test_non_image_photo_is_rejected(client, tenant_a):
    enable(tenant_a)
    pdf = SimpleUploadedFile("x.pdf", b"%PDF-1.4", content_type="application/pdf")
    assert client.post(qr_url(tenant_a), form_data(client, tenant_a, photo=pdf)).status_code == 200
    assert not requests_of(tenant_a)


def test_public_form_is_csrf_protected(tenant_a):
    from django.test import Client

    enable(tenant_a)
    strict = Client(enforce_csrf_checks=True)
    data = form_data(strict, tenant_a)
    strict.cookies.clear()
    assert strict.post(qr_url(tenant_a), data).status_code == 403
    assert not requests_of(tenant_a)


# --- Foto del aviso con su propio target (REQ-02) --------------------------------------


def test_request_photo_is_served_by_the_generic_file_view(client, tenant_a, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    enable(tenant_a)
    with tenant_context(tenant_a.org):
        report_anonymous_failure(asset=tenant_a.asset, name="Juan", description="x", photo=photo())
        attachment = Attachment.objects.get()
    assert attachment.target_type == TargetType.WORK_REQUEST
    login(client, tenant_a.admin)
    assert client.get(reverse("attachment", args=[attachment.pk])).status_code == 200


def _migration_apps():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    loader = MigrationExecutor(connection).loader
    return loader.project_state(("attachments", "0005_work_request_target")).apps


def test_migration_moves_old_anonymous_photos_to_the_request_target(tenant_a, tenant_b, settings, tmp_path):
    """Filas antiguas (`work_order` + id de la solicitud) pasan a `work_request`; nada más cambia."""
    import importlib

    from apps.core.tenancy import platform_context
    from apps.work.services import add_attachment, create_work_order

    migration = importlib.import_module("apps.attachments.migrations.0005_work_request_target")
    settings.MEDIA_ROOT = tmp_path
    enable(tenant_a)
    enable(tenant_b)
    with tenant_context(tenant_b.org):
        request_b = report_anonymous_failure(asset=tenant_b.asset, name="B", description="x")
    with tenant_context(tenant_a.org):
        request = report_anonymous_failure(asset=tenant_a.asset, name="Juan", description="x", photo=photo())
        old = Attachment.objects.get()
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
        wo_photo = add_attachment(work_order=wo, uploaded_file=photo(), by=None)
        stray = add_attachment(work_order=wo, uploaded_file=photo(), by=None)
        Attachment.objects.filter(pk=old.pk).update(target_type=TargetType.WORK_ORDER)  # como antes
        Attachment.objects.filter(pk=stray.pk).update(target_id=request_b.pk)  # id de otra organización

    with platform_context():
        migration.forwards(_migration_apps(), None)
    with tenant_context(tenant_a.org):
        rows = dict(Attachment.objects.values_list("pk", "target_type"))
    assert rows == {
        old.pk: TargetType.WORK_REQUEST,
        wo_photo.pk: TargetType.WORK_ORDER,
        stray.pk: TargetType.WORK_ORDER,
    }
    assert Attachment.all_tenants.get(pk=old.pk).target_id == request.pk  # plataforma: solo lectura

    with platform_context():
        migration.backwards(_migration_apps(), None)
    with tenant_context(tenant_a.org):
        assert Attachment.objects.get(pk=old.pk).target_type == TargetType.WORK_ORDER
