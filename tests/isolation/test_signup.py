"""ORG-02: la organización creada por autoregistro nace vacía y no ve datos de otras."""

import pytest
from django.urls import reverse

from apps.accounts.services import complete_signup, request_signup
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context


@pytest.fixture
def new_signup(db):
    tokens = []
    request_signup(
        email="nuevo@z.cl",
        full_name="Nuevo",
        organization_name="Zeta",
        password="una-clave-bien-larga-2026",
        confirm_url=lambda t: tokens.append(t) or "u",
        login_url="l",
    )
    return complete_signup(token=tokens[0])


def test_new_organization_does_not_see_other_assets(client, tenant_a, new_signup):
    user, org = new_signup
    with tenant_context(org):
        assert Asset.objects.count() == 0
    client.force_login(user)
    response = client.get(reverse("asset_list"))
    assert response.status_code == 200
    assert tenant_a.asset.code not in response.content.decode()
    assert client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).status_code == 404
