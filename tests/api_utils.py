"""Ayudas para probar la API v1 con tokens de organización."""

from rest_framework.test import APIClient

from apps.api.services import create_token
from apps.core.tenancy import tenant_context


def make_token(tenant, scope="write", **kwargs):
    with tenant_context(tenant.org):
        return create_token(membership=tenant.admin, name="ERP", scope=scope, **kwargs)


def api_client(tenant, scope="write", **kwargs):
    _token, raw = make_token(tenant, scope, **kwargs)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client


FIELD_API = "/field/api"


def field_client(membership, **kwargs):
    """Cliente de la API de terreno (`/field/api/`): la sesión de la web, sin token."""
    client = APIClient(**kwargs)
    client.force_login(membership.user)
    return client
