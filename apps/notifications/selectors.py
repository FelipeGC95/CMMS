from .models import Kind, Notification, NotificationPreference


def notification_list(*, membership):
    """Notificaciones propias en la app, de la más reciente a la más antigua."""
    return Notification.objects.filter(recipient=membership.user, in_app=True).order_by("-created_at")


def unread_count(*, membership) -> int:
    """Contador de la campana: una sola consulta sobre el índice de no leídas."""
    return Notification.objects.filter(recipient=membership.user, in_app=True, read_at__isnull=True).count()


def preference_rows(*, membership) -> list[dict]:
    """Una fila por tipo con sus dos canales (los no guardados, activos)."""
    saved = {
        kind: (in_app, email)
        for kind, in_app, email in NotificationPreference.objects.filter(user=membership.user).values_list(
            "kind", "in_app", "email"
        )
    }
    return [
        {"kind": kind, "label": label, "in_app": in_app, "email": email}
        for kind, label in Kind.choices
        for in_app, email in [saved.get(kind, (True, True))]
    ]
