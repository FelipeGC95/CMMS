"""Manuales PDF de activos y clases de activo, con texto por página (AST-06 CA3)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse

from apps.attachments import services
from apps.attachments import tasks as attachment_tasks
from apps.attachments.models import Attachment, DocumentPage, Purpose, TargetType, TextStatus
from apps.attachments.selectors import attachments_for, manuals_for_asset, search_manual_pages, snippet
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from tests.conftest import login, make_manual, pdf_bytes


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _upload(target_type, target_id, data, *, by, name="manual.pdf", content_type="application/pdf"):
    return services.add_manual(
        target_type=target_type,
        target_id=target_id,
        uploaded_file=SimpleUploadedFile(name, data, content_type=content_type),
        by=by,
    )


def test_multi_page_pdf_text_is_stored_per_page(in_a):
    manual = make_manual(
        in_a.asset,
        ["Purgar el compresor cada semana", "", "Cambiar aceite a las 2000 horas"],
        by=in_a.admin.user,
    )
    manual.refresh_from_db()
    assert manual.purpose == Purpose.MANUAL
    assert manual.text_status == TextStatus.DONE
    assert manual.page_count == 3
    pages = list(DocumentPage.objects.filter(attachment=manual).values_list("page_number", "text"))
    # La página sin texto no se guarda; la numeración respeta el PDF.
    assert [number for number, _ in pages] == [1, 3]
    assert "aceite" in pages[1][1]


def test_scanned_pdf_without_text_is_marked_no_text(in_a):
    manual = make_manual(in_a.asset, ["", ""], by=in_a.admin.user)
    manual.refresh_from_db()
    assert manual.text_status == TextStatus.NO_TEXT
    assert manual.page_count == 2
    assert not DocumentPage.objects.filter(attachment=manual).exists()


def test_broken_pdf_is_marked_failed(in_a):
    manual = _upload(TargetType.ASSET, in_a.asset.pk, b"%PDF-1.4 roto", by=in_a.admin.user)
    assert manual.text_status == TextStatus.PENDING
    assert services.extract_document_text(attachment=manual) == TextStatus.FAILED
    manual.refresh_from_db()
    assert manual.text_status == TextStatus.FAILED


def test_extraction_is_idempotent(in_a):
    manual = make_manual(in_a.asset, ["uno", "dos"], by=in_a.admin.user)
    services.extract_document_text(attachment=manual)
    assert DocumentPage.objects.filter(attachment=manual).count() == 2


def test_page_cap(in_a, monkeypatch):
    monkeypatch.setattr(services, "MAX_EXTRACTED_PAGES", 2)
    manual = make_manual(in_a.asset, ["a1", "b2", "c3"], by=in_a.admin.user)
    manual.refresh_from_db()
    assert manual.page_count == 3
    assert DocumentPage.objects.filter(attachment=manual).count() == 2


@pytest.mark.parametrize(
    "data,content_type",
    [(b"no es un pdf", "application/pdf"), (pdf_bytes(["x"]), "image/jpeg")],
    ids=["not-a-pdf", "pdf-as-jpeg"],  # el PDF lleva la hora: con ids fijos xdist recolecta igual
)
def test_only_pdf_manuals_are_accepted(in_a, data, content_type):
    with pytest.raises(services.AttachmentError):
        _upload(TargetType.ASSET, in_a.asset.pk, data, by=in_a.admin.user, content_type=content_type)
    assert not Attachment.objects.exists()


def test_manual_targets_are_asset_or_class(in_a):
    with pytest.raises(services.AttachmentError):
        _upload(TargetType.WORK_ORDER, in_a.asset.pk, pdf_bytes(["x"]), by=in_a.admin.user)


def test_upload_is_idempotent_by_client_uuid(in_a):
    import uuid

    key = uuid.uuid4()
    for _ in range(2):
        services.add_manual(
            target_type=TargetType.ASSET,
            target_id=in_a.asset.pk,
            uploaded_file=SimpleUploadedFile("m.pdf", pdf_bytes(["x"]), content_type="application/pdf"),
            by=in_a.admin.user,
            client_uuid=key,
        )
    assert Attachment.objects.filter(purpose=Purpose.MANUAL).count() == 1


def test_sync_extraction_runs_on_commit_without_queue(in_a, settings, django_capture_on_commit_callbacks):
    settings.DOCUMENT_TEXT_USE_QUEUE = False
    with django_capture_on_commit_callbacks(execute=True):
        manual = _upload(TargetType.ASSET, in_a.asset.pk, pdf_bytes(["filtro de aire"]), by=in_a.admin.user)
    manual.refresh_from_db()
    assert manual.text_status == TextStatus.DONE


def test_queue_defers_task_with_organization(in_a, settings, monkeypatch, django_capture_on_commit_callbacks):
    settings.DOCUMENT_TEXT_USE_QUEUE = True
    deferred = []
    monkeypatch.setattr(attachment_tasks.extract_document_text, "defer", lambda **kw: deferred.append(kw))
    with django_capture_on_commit_callbacks(execute=True):
        manual = _upload(TargetType.ASSET, in_a.asset.pk, pdf_bytes(["x"]), by=in_a.admin.user)
    assert deferred == [{"organization_id": str(in_a.org.pk), "attachment_id": str(manual.pk)}]
    manual.refresh_from_db()
    assert manual.text_status == TextStatus.PENDING


def test_task_extracts_within_its_organization(tenant_a):
    with tenant_context(tenant_a.org):
        manual = _upload(TargetType.ASSET, tenant_a.asset.pk, pdf_bytes(["correa"]), by=tenant_a.admin.user)
    result = attachment_tasks.extract_document_text(
        organization_id=str(tenant_a.org.pk), attachment_id=str(manual.pk)
    )
    assert result == TextStatus.DONE


def test_command_extracts_pending_manuals(tenant_a, tenant_b):
    for tenant in (tenant_a, tenant_b):
        with tenant_context(tenant.org):
            _upload(TargetType.ASSET, tenant.asset.pk, pdf_bytes(["rodamiento"]), by=tenant.admin.user)
    call_command("extract_document_text")
    for tenant in (tenant_a, tenant_b):
        with tenant_context(tenant.org):
            assert Attachment.objects.get().text_status == TextStatus.DONE


def test_remove_manual_deletes_pages_and_audits(in_a):
    manual = make_manual(in_a.asset, ["uno"], by=in_a.admin.user)
    services.remove_manual(attachment=manual, by=in_a.admin.user)
    assert not Attachment.objects.exists()
    assert not DocumentPage.objects.exists()
    assert AuditEvent.objects.filter(action=AuditEvent.Action.DELETE).exists()


def test_class_manuals_apply_to_every_asset_of_the_class(in_a):
    from apps.assets.models import Asset

    other = Asset.objects.create(code="CMP-02", name="Compresor 2", asset_class=in_a.asset.asset_class)
    make_manual(in_a.asset.asset_class, ["clase"], by=in_a.admin.user, name="clase.pdf")
    make_manual(in_a.asset, ["propio"], by=in_a.admin.user, name="propio.pdf")
    assert {m.filename for m in manuals_for_asset(asset=in_a.asset)} == {"clase.pdf", "propio.pdf"}
    assert [m.filename for m in manuals_for_asset(asset=other)] == ["clase.pdf"]
    # Los manuales no se mezclan con "Documentos y fotos".
    assert not attachments_for(target_type=TargetType.ASSET, target_id=in_a.asset.pk).exists()


def test_search_finds_page_and_all_words(in_a):
    make_manual(in_a.asset, ["Revisar correa del motor", "Cambiar aceite del compresor"], by=in_a.admin.user)
    hits = list(search_manual_pages(membership=in_a.admin, query="aceite compresor"))
    assert [hit.page_number for hit in hits] == [2]
    assert list(search_manual_pages(membership=in_a.admin, query="aceite correa")) == []
    assert list(search_manual_pages(membership=in_a.admin, query="  ")) == []


def test_snippet_centers_on_the_match():
    text = "x " * 300 + "presión de aceite" + " y" * 300
    result = snippet(text, ["aceite"])
    assert "aceite" in result and result.startswith("…") and result.endswith("…")


def test_asset_page_lists_own_and_class_manuals_labelled(client, in_a):
    make_manual(in_a.asset.asset_class, ["clase"], by=in_a.admin.user, name="clase.pdf")
    make_manual(in_a.asset, [""], by=in_a.admin.user, name="escaneado.pdf")
    login(client, in_a.viewer)
    html = client.get(reverse("asset_detail", args=[in_a.asset.pk])).content.decode()
    assert "clase.pdf" in html and "escaneado.pdf" in html
    assert "De la clase" in html and "De este activo" in html
    assert "PDF escaneado" in html
    assert reverse("manual_search") in html


def test_upload_view_and_remove_flow(client, in_a):
    login(client, in_a.admin)
    url = reverse("asset_class_manual_add", args=[in_a.asset.asset_class_id])
    upload = SimpleUploadedFile("bomba.pdf", pdf_bytes(["impulsor"]), content_type="application/pdf")
    response = client.post(url, {"file": upload})
    assert response.status_code == 302
    manual = Attachment.objects.get(purpose=Purpose.MANUAL)
    assert manual.target_type == TargetType.ASSET_CLASS
    remove_url = reverse("manual_remove", args=[manual.pk])
    assert "bomba.pdf" in client.get(remove_url).content.decode()  # confirmación sin JS
    assert client.post(remove_url).status_code == 302
    assert not Attachment.objects.exists()


def test_search_view_shows_file_page_target_and_snippet(client, in_a):
    make_manual(in_a.asset, ["intro", "Tensar la correa del ventilador"], by=in_a.admin.user, name="v.pdf")
    login(client, in_a.tech)
    html = client.get(reverse("manual_search"), {"q": "correa"}).content.decode()
    assert "v.pdf" in html and "Página 2" in html and in_a.asset.code in html and "ventilador" in html


def test_class_manual_file_is_served_to_members(client, in_a):
    manual = make_manual(in_a.asset.asset_class, ["x"], by=in_a.admin.user)
    login(client, in_a.viewer)
    assert client.get(reverse("attachment", args=[manual.pk])).status_code == 200
