import datetime
import zoneinfo

from apps.accounts.permissions import Perm, require_perm

from .models import AuditEvent


def audit_events(*, membership, date_from=None, date_to=None, actor="", action="", target_type=None):
    """Eventos de la organización activa, del más reciente al más antiguo (X-01).

    Las fechas se interpretan en la zona horaria de la organización.
    """
    require_perm(membership, Perm.AUDIT_VIEW)
    tz = zoneinfo.ZoneInfo(membership.organization.timezone)
    events = AuditEvent.objects.select_related("target_type")
    if date_from:
        events = events.filter(created_at__gte=_start_of(date_from, tz))
    if date_to:
        events = events.filter(created_at__lt=_start_of(date_to + datetime.timedelta(days=1), tz))
    if actor:
        events = events.filter(actor_repr__icontains=actor)
    if action:
        events = events.filter(action=action)
    if target_type:
        events = events.filter(target_type=target_type)
    return events.order_by("-created_at")


def _start_of(day: datetime.date, tz) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)


def audited_types():
    """Tipos de objeto con eventos en la organización activa (para el filtro)."""
    from django.contrib.contenttypes.models import ContentType

    ids = AuditEvent.objects.values_list("target_type", flat=True).distinct()
    return ContentType.objects.filter(pk__in=ids)
