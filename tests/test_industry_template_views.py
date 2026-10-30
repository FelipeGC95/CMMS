"""Pantalla «Cargar plantilla de rubro» (ORG-03 CA3, PM-07): elegir, ver qué se crea y confirmar."""

from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.maintenance.models import MaintenancePlan
from apps.maintenance.templates_library import load_template
from tests.conftest import login


def test_list_shows_the_four_industries_and_links_from_plans(client, tenant_a):
    login(client, tenant_a.admin)
    html = client.get(reverse("industry_templates")).content.decode()
    for name in ("Flota y equipo móvil", "Planta industrial", "Alimentos y agroindustria", "Edificios"):
        assert name in html
    assert reverse("industry_templates") in client.get(reverse("plan_list")).content.decode()
    assert reverse("industry_templates") in client.get(reverse("menu")).content.decode()


def test_preview_writes_nothing_and_confirm_applies(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("industry_template", args=["fleet"])
    response = client.get(url)
    assert response.status_code == 200
    html = response.content.decode()
    assert "GRH-C-250H" in html and "250 h o 3 meses" in html and "No arranca" in html
    with tenant_context(tenant_a.org):
        assert not MaintenancePlan.objects.filter(code="GRH-C-250H").exists()
    response = client.post(url, follow=True)
    assert response.redirect_chain[-1][0] == reverse("plan_list")
    assert "Plantilla «Flota y equipo móvil» cargada" in response.content.decode()
    with tenant_context(tenant_a.org):
        assert MaintenancePlan.objects.get(code="GRH-C-250H").current_version is not None
    # Segunda vez: nada nuevo.
    assert "No hay nada nuevo que crear" in client.get(url).content.decode()
    response = client.post(url, follow=True)
    assert "no se creó nada" in response.content.decode()


def test_draft_mark_is_only_for_platform_admins(client, tenant_a, monkeypatch):
    # Las cuatro están revisadas (P-08): se simula una versión nueva en borrador.
    data = load_template("food")
    monkeypatch.setitem(data, "review_status", "draft")
    monkeypatch.setitem(data, "review_note", "Borrador — revisar antes del piloto")
    login(client, tenant_a.admin)
    url = reverse("industry_template", args=["food"])
    assert "Borrador — revisar antes del piloto" not in client.get(url).content.decode()
    tenant_a.admin.user.is_staff = True
    tenant_a.admin.user.save(update_fields=["is_staff"])
    assert "Borrador — revisar antes del piloto" in client.get(url).content.decode()
    assert "Borrador — revisar antes del piloto" in client.get(reverse("industry_templates")).content.decode()


def test_reviewed_templates_show_no_draft_mark(client, tenant_a):
    tenant_a.admin.user.is_staff = True
    tenant_a.admin.user.save(update_fields=["is_staff"])
    login(client, tenant_a.admin)
    assert "Borrador" not in client.get(reverse("industry_templates")).content.decode()


def test_unknown_industry_is_404(client, tenant_a):
    login(client, tenant_a.admin)
    assert client.get(reverse("industry_template", args=["mineria"])).status_code == 404
    assert client.post(reverse("industry_template", args=["mineria"])).status_code == 404


def test_management_command_applies_and_dry_run_does_not(tenant_a):
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("apply_industry_template", tenant_a.org.slug, "buildings", "--dry-run", stdout=out)
    assert "Se crearían" in out.getvalue()
    with tenant_context(tenant_a.org):
        assert not MaintenancePlan.objects.filter(code="ASC-MENS").exists()
    call_command("apply_industry_template", tenant_a.org.slug, "buildings", stdout=StringIO())
    with tenant_context(tenant_a.org):
        assert MaintenancePlan.objects.filter(code="ASC-MENS").exists()
