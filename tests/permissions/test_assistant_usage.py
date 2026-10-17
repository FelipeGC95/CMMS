"""La vista de costo del asistente (AI-05 CA2) es solo del superadmin de plataforma."""

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from tests.assistant_usage_utils import usage

URL = "admin:assistant_assistantusage_changelist"


def test_anonymous_is_sent_to_login(client, db):
    response = client.get(reverse(URL))
    assert response.status_code == 302


@pytest.mark.parametrize("with_all_permissions", [False, True])
def test_organization_admin_with_admin_access_cannot_see_costs(client, tenant_a, with_all_permissions):
    # Administrador de la organización con acceso al admin de Django (como el de la demo),
    # incluso con todos los permisos de modelo: el costo es de plataforma.
    usage(tenant_a, at=timezone.now(), output_tokens=1000)
    user = tenant_a.admin.user
    user.is_staff = True
    user.save()
    if with_all_permissions:
        user.user_permissions.set(Permission.objects.filter(content_type__app_label="assistant"))
    client.force_login(user)

    assert client.get(reverse(URL)).status_code == 403
    assert reverse(URL) not in client.get(reverse("admin:index")).content.decode()


def test_member_without_admin_access_is_rejected(client, tenant_a):
    client.force_login(tenant_a.tech.user)
    response = client.get(reverse(URL))
    assert response.status_code == 302  # el admin pide un usuario staff
    assert "login" in response["Location"]


def test_report_has_no_detail_add_or_delete_pages(db):
    from django.urls import NoReverseMatch

    for name in ("add", "change", "delete", "history"):
        with pytest.raises(NoReverseMatch):
            reverse(f"admin:assistant_assistantusage_{name}", args=[] if name == "add" else ["x"])
