"""Barra superior compacta y sección "Mi cuenta" de /menu/ (ADR-017).

La barra muestra solo el nombre de pila (sin rol) y el nombre corto de la
organización; el rol, el correo, el nombre completo y "Cambiar contraseña"
viven en /menu/.
"""

import re

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.organizations.models import Organization
from tests.conftest import login

NAVBAR = re.compile(r'<header class="navbar">(.*?)</header>', re.S)
ACCOUNT = re.compile(r'<section class="card" id="account"[^>]*>(.*?)</section>', re.S)


@pytest.mark.parametrize(
    ("name", "short"),
    [
        ("Alimentos del Sur SpA", "Alimentos del Sur"),
        ("Maestranza Norte Ltda.", "Maestranza Norte"),
        ("Pesquera Austral S.A.", "Pesquera Austral"),
        ("Servicios Pérez, S.p.A.", "Servicios Pérez"),
        ("Taller Rojas EIRL", "Taller Rojas"),
        ("Spa Termal Puyehue", "Spa Termal Puyehue"),
        ("SpA", "SpA"),
    ],
)
def test_organization_short_name_drops_legal_suffix(name, short):
    assert Organization(name=name).short_name == short


def test_header_shows_first_name_only_and_links_to_account(client, tenant_a):
    login(client, tenant_a.admin)
    header = NAVBAR.search(client.get(reverse("dashboard")).content.decode()).group(1)
    assert tenant_a.admin.get_role_display() not in header
    assert f'href="{reverse("menu")}#account"' in header
    assert reverse("account_change_password") not in header


def test_header_org_switch_uses_short_names(client, tenant_a, tenant_b):
    Organization.objects.filter(pk=tenant_b.org.pk).update(name="Maestranza Norte Ltda.")
    Membership.objects.create(user=tenant_a.admin.user, organization=tenant_b.org, role="viewer")
    login(client, tenant_a.admin)
    header = NAVBAR.search(client.get(reverse("dashboard")).content.decode()).group(1)
    assert ">Maestranza Norte</option>" in header
    menu = client.get(reverse("menu")).content.decode()
    assert "Maestranza Norte Ltda." in menu


def test_menu_account_separates_name_from_password_link(client, tenant_a):
    user = tenant_a.admin.user
    login(client, tenant_a.admin)
    account = ACCOUNT.search(client.get(reverse("menu")).content.decode()).group(1)
    name = user.full_name or user.email
    # El nombre es texto, no parte del enlace; el enlace dice solo "Cambiar contraseña".
    assert f'<span class="account__name">{name}</span>' in account
    assert tenant_a.admin.get_role_display() in account
    link = re.search(rf'<a href="{reverse("account_change_password")}">(.*?)</a>', account, re.S).group(1)
    assert re.sub(r"<svg.*?</svg>", "", link, flags=re.S).strip() == "Cambiar contraseña"
    logout = re.search(r'<button class="btn btn--secondary" type="submit">(.*?)</button>', account, re.S)
    assert "<svg" in logout.group(1) and "Salir" in logout.group(1)
