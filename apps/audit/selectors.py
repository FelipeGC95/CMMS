from apps.accounts.permissions import Perm, require_perm

from .models import AuditEvent


def audit_events(*, membership, date_from=None, date_to=None, actor="", action="", target_type=None):
    """Eventos de la organización activa, del más reciente al más antiguo (X-01)."""
    require_perm(membership, Perm.AUDIT_VIEW)
    events = AuditEvent.objects.select_related("target_type")
    if date_from:
        events = events.filter(created_at__date__gte=date_from)
    if date_to:
        events = events.filter(created_at__date__lte=date_to)
    if actor:
        events = events.filter(actor_repr__icontains=actor)
    if action:
        events = events.filter(action=action)
    if target_type:
        events = events.filter(target_type=target_type)
    return events.order_by("-created_at")


def audited_types():
    """Tipos de objeto con eventos en la organización activa (para el filtro)."""
    from django.contrib.contenttypes.models import ContentType

    ids = AuditEvent.objects.values_list("target_type", flat=True).distinct()
    return ContentType.objects.filter(pk__in=ids)
