"""Lecturas de webhooks (X-08): solo para quien administra integraciones."""

from django.shortcuts import get_object_or_404

from apps.accounts.permissions import Perm, require_perm

from .models import WebhookDelivery, WebhookEndpoint


def endpoint_list(*, membership):
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    return WebhookEndpoint.objects.select_related("created_by").order_by("-is_active", "-created_at")


def get_endpoint(*, membership, pk) -> WebhookEndpoint:
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    return get_object_or_404(WebhookEndpoint, pk=pk)


def delivery_list(*, membership, endpoint: WebhookEndpoint):
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    return (
        WebhookDelivery.objects.filter(endpoint=endpoint)
        .defer("payload", "response_body")
        .order_by("-created_at")
    )


def get_delivery(*, membership, endpoint: WebhookEndpoint, pk) -> WebhookDelivery:
    """Una entrega del destino (de la organización del contexto: una ajena da 404)."""
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    return get_object_or_404(
        WebhookDelivery.objects.select_related("created_by", "redelivery_of"), endpoint=endpoint, pk=pk
    )


def redelivery_list(*, membership, delivery: WebhookDelivery) -> list[WebhookDelivery]:
    """Reenvíos manuales del mismo evento al mismo destino, del más reciente al más antiguo."""
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    return list(
        WebhookDelivery.objects.filter(endpoint_id=delivery.endpoint_id, event_id=delivery.event_id)
        .exclude(pk=delivery.pk)
        .defer("payload", "response_body")
        .order_by("-created_at")[:20]
    )
