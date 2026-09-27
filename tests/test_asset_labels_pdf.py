"""Etiquetas QR en PDF, varias por hoja A4 (AST-05 CA2)."""

import io
import re

import pytest
from django.urls import reverse
from pypdf import PdfReader

from apps.assets.labels import LAYOUTS, MAX_LABELS, render_labels_pdf
from apps.assets.models import Asset
from apps.assets.selectors import label_assets
from apps.core.tenancy import tenant_context
from apps.organizations.services import set_organization_logo
from tests.conftest import login
from tests.test_org_logo import image_upload


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def pdf_pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type /Page[^s]", pdf))


def pdf_text(pdf: bytes) -> str:
    """Texto dibujado en todas las páginas (las fuentes TTF incrustadas no dejan texto plano)."""
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() for page in reader.pages)


def add_assets(tenant, n, prefix="E"):
    with tenant_context(tenant.org):
        return [
            Asset.objects.create(
                code=f"{prefix}-{i:03d}", name=f"Equipo {i}", asset_class=tenant.asset.asset_class
            )
            for i in range(n)
        ]


def _render(tenant, *, layout="3x8"):
    with tenant_context(tenant.org):
        assets = label_assets(membership=tenant.admin)
        tenant.org.refresh_from_db()
        return render_labels_pdf(
            assets=assets,
            organization=tenant.org,
            qr_url=lambda token: f"https://cmms.test/q/{token}/",
            layout_key=layout,
        )


@pytest.mark.parametrize(
    ("extra", "layout", "pages"), [(0, "3x8", 1), (23, "3x8", 1), (24, "3x8", 2), (20, "2x5", 3)]
)
def test_one_label_per_asset_and_pages_by_layout(tenant_a, extra, layout, pages):
    add_assets(tenant_a, extra)
    pdf = _render(tenant_a, layout=layout)
    assert pdf.startswith(b"%PDF")
    total = extra + 1
    assert pdf_pages(pdf) == pages == -(-total // LAYOUTS[layout].per_page)
    text = pdf_text(pdf)
    assert text.count("CMP-01") == 1
    assert sum(text.count(f"E-{i:03d}") for i in range(extra)) == extra


def test_without_logo_the_label_shows_the_organization_name(tenant_a):
    pdf = _render(tenant_a)
    assert b"/Subtype /Image" not in pdf
    assert tenant_a.org.name in pdf_text(pdf)


def test_with_logo_the_label_shows_the_image_once(tenant_a):
    add_assets(tenant_a, 5)
    with tenant_context(tenant_a.org):
        set_organization_logo(membership=tenant_a.admin, uploaded_file=image_upload())
    pdf = _render(tenant_a)
    # El logo se incrusta una vez (imagen + su máscara de transparencia) y se reutiliza en las 6.
    assert pdf.count(b"/Subtype /Image") == 2
    assert pdf.count(b"/SMask") == 1
    assert tenant_a.org.name not in pdf_text(pdf)


def test_missing_logo_file_falls_back_to_the_name(tenant_a):
    with tenant_context(tenant_a.org):
        org = set_organization_logo(membership=tenant_a.admin, uploaded_file=image_upload())
    org.logo.storage.delete(org.logo.name)
    assert tenant_a.org.name in pdf_text(_render(tenant_a))


def test_qr_points_to_the_token_url_not_the_internal_id(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("asset_labels_pdf"))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert "inline" in response["Content-Disposition"]
    assert str(tenant_a.asset.pk) not in pdf_text(response.content)


def test_view_prints_only_the_selected_assets(client, tenant_a):
    extra = add_assets(tenant_a, 3)
    login(client, tenant_a.admin)
    response = client.get(
        reverse("asset_labels_pdf"), {"scope": "selected", "ids": [str(extra[0].pk), str(extra[2].pk), "x"]}
    )
    text = pdf_text(response.content)
    assert "E-000" in text and "E-002" in text
    assert "E-001" not in text and "CMP-01" not in text


def test_view_prints_the_current_filter(client, tenant_a):
    add_assets(tenant_a, 3)
    login(client, tenant_a.admin)
    text = pdf_text(client.get(reverse("asset_labels_pdf"), {"scope": "filtered", "q": "E-00"}).content)
    assert "E-000" in text and "CMP-01" not in text


def test_selected_without_marks_returns_to_the_list_with_a_message(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("asset_labels_pdf"), {"scope": "selected", "q": "CMP"}, follow=True)
    assert response.redirect_chain[-1][0] == f"{reverse('asset_list')}?q=CMP&labels=1"
    assert "Marca al menos un activo" in response.content.decode()


def test_over_the_cap_returns_a_clear_message(client, tenant_a, monkeypatch):
    monkeypatch.setattr("apps.assets.views.MAX_LABELS", 2)
    add_assets(tenant_a, 2)
    login(client, tenant_a.admin)
    response = client.get(reverse("asset_labels_pdf"), follow=True)
    assert response["Content-Type"].startswith("text/html")
    assert "un PDF admite hasta 2 etiquetas" in response.content.decode()


def test_cap_is_sane():
    assert MAX_LABELS == 500


def test_retired_assets_are_not_printed(client, tenant_a):
    """Igual que la lista: solo activos vigentes."""
    extra = add_assets(tenant_a, 1)
    with tenant_context(tenant_a.org):
        Asset.objects.filter(pk=extra[0].pk).update(is_active=False)
    login(client, tenant_a.admin)
    assert "E-000" not in pdf_text(client.get(reverse("asset_labels_pdf")).content)


def test_old_labels_page_redirects_to_the_selection(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("asset_labels"))
    assert response.status_code == 302
    assert response["Location"] == f"{reverse('asset_list')}?labels=1"


def test_list_in_labels_mode_shows_checkboxes_and_the_form(client, tenant_a):
    login(client, tenant_a.admin)
    body = client.get(reverse("asset_list"), {"labels": "1"}).content.decode()
    assert 'id="labels-form"' in body
    assert f'value="{tenant_a.asset.pk}" form="labels-form"' in body
    assert 'id="labels-form"' not in client.get(reverse("asset_list")).content.decode()


def test_many_labels_use_a_bounded_number_of_queries(client, tenant_a, django_assert_max_num_queries):
    add_assets(tenant_a, 60)
    with tenant_context(tenant_a.org):
        set_organization_logo(membership=tenant_a.admin, uploaded_file=image_upload())
    login(client, tenant_a.admin)
    with django_assert_max_num_queries(8):
        response = client.get(reverse("asset_labels_pdf"), {"size": "2x5"})
    assert pdf_pages(response.content) == 7


def test_names_beyond_latin1_print_with_the_embedded_font(tenant_a):
    name = "Bomba Ñandú – Nº 3 ø25 ✓"
    with tenant_context(tenant_a.org):
        Asset.objects.filter(pk=tenant_a.asset.pk).update(name=name)
    pdf = _render(tenant_a, layout="2x5")
    assert b"DejaVuSans" in pdf  # fuente TTF incrustada, no Helvetica
    text = pdf_text(pdf)
    assert "Bomba Ñandú – Nº 3 ø25 ✓" in " ".join(text.split())


# --- Selección de etiquetas entre páginas (sesión) ---


def _select(client, *, page, marked, action="save", **extra):
    data = {"page_ids": [str(a.pk) for a in page], "ids": [str(a.pk) for a in marked], "action": action}
    return client.post(reverse("asset_labels_select"), {**data, **extra})


def test_marks_persist_across_pages_and_print_together(client, tenant_a):
    extra = add_assets(tenant_a, 30)
    login(client, tenant_a.admin)
    _select(client, page=extra[:25], marked=[extra[0]])
    _select(client, page=extra[25:], marked=[extra[29]])
    body = client.get(reverse("asset_list"), {"labels": "1", "page": "2"}).content.decode()
    assert "2 seleccionados" in body
    assert f'value="{extra[29].pk}" form="labels-form" checked' in body
    text = pdf_text(client.get(reverse("asset_labels_pdf"), {"scope": "selected"}).content)
    assert "E-000" in text and "E-029" in text and "E-001" not in text


def test_unticking_on_a_page_removes_only_that_page_marks(client, tenant_a):
    extra = add_assets(tenant_a, 4)
    login(client, tenant_a.admin)
    _select(client, page=extra[:2], marked=extra[:2])
    _select(client, page=extra[2:], marked=extra[2:])
    _select(client, page=extra[:2], marked=[extra[1]])
    text = pdf_text(client.get(reverse("asset_labels_pdf"), {"scope": "selected"}).content)
    assert "E-000" not in text and "E-001" in text and "E-002" in text and "E-003" in text


def test_clear_selection(client, tenant_a):
    extra = add_assets(tenant_a, 2)
    login(client, tenant_a.admin)
    _select(client, page=extra, marked=extra)
    response = _select(client, page=[], marked=[], action="clear", next=f"{reverse('asset_list')}?labels=1")
    assert response["Location"] == f"{reverse('asset_list')}?labels=1"
    body = client.get(reverse("asset_list"), {"labels": "1"}).content.decode()
    assert "0 seleccionados" in body


def test_htmx_toggle_returns_the_counter(client, tenant_a):
    extra = add_assets(tenant_a, 2)
    login(client, tenant_a.admin)
    data = {"page_ids": [str(a.pk) for a in extra], "ids": [str(extra[0].pk), str(extra[1].pk)]}
    response = client.post(reverse("asset_labels_select"), data, headers={"HX-Request": "true"})
    body = response.content.decode()
    assert response.status_code == 200 and 'id="labels-count"' in body and "2 seleccionados" in body


def test_print_buttons_without_js_save_then_redirect_to_the_pdf(client, tenant_a):
    extra = add_assets(tenant_a, 2)
    login(client, tenant_a.admin)
    response = _select(client, page=extra, marked=[extra[1]], action="print_selected", size="2x5")
    assert response["Location"] == f"{reverse('asset_labels_pdf')}?size=2x5&scope=selected"
    text = pdf_text(client.get(response["Location"]).content)
    assert "E-001" in text and "E-000" not in text
    response = _select(client, page=[], marked=[], action="print_filtered", q="E-00")
    assert response["Location"] == f"{reverse('asset_labels_pdf')}?size=3x8&scope=filtered&q=E-00"


def test_selection_is_capped(client, tenant_a, monkeypatch):
    monkeypatch.setattr("apps.assets.views.MAX_LABELS", 2)
    extra = add_assets(tenant_a, 3)
    login(client, tenant_a.admin)
    response = _select(client, page=extra, marked=extra)
    assert len(client.session[f"label_selection:{tenant_a.org.pk}"]) == 2
    assert response.status_code == 302


def test_next_outside_the_list_is_ignored(client, tenant_a):
    login(client, tenant_a.admin)
    response = _select(client, page=[], marked=[], next="https://evil.test/")
    assert response["Location"] == f"{reverse('asset_list')}?labels=1"
