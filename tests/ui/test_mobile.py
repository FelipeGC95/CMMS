"""Uso completo desde un smartphone (ADR-017, docs/06_sistema_de_diseno.md §6).

Bajo 720 px cada fila de una `.table` se apila como tarjeta y `data-label`
reemplaza al encabezado. Toda celda lo declara; `data-label=""` marca una
celda que se explica sola (nombre, estado, acciones).
"""

import re
from pathlib import Path

import pytest
from django.urls import reverse

from tests.conftest import login

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"

TABLE = re.compile(r'<table\b[^>]*class="[^"]*\btable\b[^"]*"[^>]*>(.*?)</table>', re.S)
TD = re.compile(r"<td\b[^>]*>")


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.relative_to(TEMPLATES)))
def test_table_cells_declare_data_label(path):
    source = path.read_text(encoding="utf-8")
    missing = [td for table in TABLE.findall(source) for td in TD.findall(table) if "data-label" not in td]
    assert not missing, f"Celdas sin data-label en {path.name}: {missing}"


@pytest.mark.parametrize("layout", ["base.html", "allauth/layouts/base.html"])
def test_layouts_declare_viewport(layout):
    source = (TEMPLATES / layout).read_text(encoding="utf-8")
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in source


NAV_LINKS = re.compile(r'<nav class="navbar__links"[^>]*>(.*?)</nav>', re.S)
BOTTOM_NAV = re.compile(r'<nav class="bottom-nav"[^>]*>(.*?)</nav>', re.S)
HREF = re.compile(r'href="([^"?#]+)')


@pytest.mark.parametrize("role", ["admin", "tech", "viewer"])
def test_every_desktop_destination_is_reachable_on_mobile(client, tenant_a, role):
    """Todo destino del menú de escritorio tiene acceso en la navegación móvil (ADR-017)."""
    membership = getattr(tenant_a, role)
    membership.user.is_staff = role == "admin"
    membership.user.save(update_fields=["is_staff"])
    login(client, membership)

    page = client.get(reverse("dashboard")).content.decode()
    desktop = set(HREF.findall(NAV_LINKS.search(page).group(1)))
    mobile = set(HREF.findall(BOTTOM_NAV.search(page).group(1)))
    assert reverse("menu") in mobile

    menu = client.get(reverse("menu"))
    assert menu.status_code == 200
    mobile |= set(HREF.findall(menu.content.decode()))
    assert desktop <= mobile, f"Destinos sin acceso móvil: {desktop - mobile}"


def test_menu_offers_organization_switch_when_member_of_several(client, tenant_a, tenant_b):
    from apps.accounts.models import Membership

    Membership.objects.create(user=tenant_a.admin.user, organization=tenant_b.org, role="viewer")
    login(client, tenant_a.admin)
    body = client.get(reverse("menu")).content.decode()
    assert reverse("switch_organization") in body
    assert tenant_b.org.name in body


def test_menu_hides_destinations_the_role_cannot_use(client, tenant_a):
    login(client, tenant_a.viewer)
    body = client.get(reverse("menu")).content.decode()
    for name in ("request_list", "team", "audit_list", "location_list"):
        assert reverse(name) not in body


def test_desktop_nav_keeps_few_links_and_groups_the_rest_in_more(client, tenant_a):
    """La barra de escritorio no debe desbordarse: lo secundario vive en "Más" (/menu/)."""
    tenant_a.admin.user.is_staff = True
    tenant_a.admin.user.save(update_fields=["is_staff"])
    login(client, tenant_a.admin)

    page = client.get(reverse("dashboard")).content.decode()
    desktop = HREF.findall(NAV_LINKS.search(page).group(1))
    assert reverse("menu") in desktop
    # Seis como máximo (Inicio, OT, Activos, Solicitudes, Inventario y Más): la barra solo se
    # muestra desde 1100 px y cabe con el nombre de la organización (capturas de INV P2).
    assert len(desktop) <= 6, f"Demasiados enlaces en la barra de escritorio: {desktop}"


def test_inventory_movements_and_reorder_live_in_more_not_in_the_desktop_bar(client, tenant_a):
    """INV P5: "Movimientos" y "Reposición" van en "Más" (y en el inicio de inventario), no en la barra."""
    login(client, tenant_a.admin)
    page = client.get(reverse("dashboard")).content.decode()
    desktop = set(HREF.findall(NAV_LINKS.search(page).group(1)))
    menu = set(HREF.findall(client.get(reverse("menu")).content.decode()))
    for name in ("inventory_movements", "inventory_reorder"):
        assert reverse(name) in menu and reverse(name) not in desktop
