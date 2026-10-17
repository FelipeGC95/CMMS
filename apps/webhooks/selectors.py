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
    return WebhookDelivery.objects.filter(endpoint=endpoint).defer("payload").order_by("-created_at")
