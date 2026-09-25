from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from apps.core.tenancy import tenant_context

from . import services
from .models import AuditEvent


@receiver(user_logged_in, dispatch_uid="audit_login")
def audit_login(sender, request, user, **kwargs):
    """Registra el login en la organización que abrirá la sesión (la misma regla que TenantMiddleware)."""
    membership = services.login_membership(user)
    if membership is None:
        return  # sin organización: evento de plataforma, fuera de la auditoría por tenant
    with tenant_context(membership.organization):
        services.record(action=AuditEvent.Action.LOGIN, target=membership, actor=user)
