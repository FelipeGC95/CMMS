"""Entrega de notificaciones en la app y por correo (X-02).

Los servicios de negocio llaman a `notify_*` dentro de su transacción; el
aviso se crea en `transaction.on_commit` (si la escritura se revierte, no hay
aviso) y dentro de la organización del contexto. El correo sale por
`send_mail`, que usa la cola cuando EMAIL_USE_QUEUE está activo
(`apps.core.mail`). Nunca se notifica a alguien sin membresía activa en la
organización ni fuera de su alcance por sitio (ORG-06).
"""

import datetime
import logging
import zoneinfo
from collections import defaultdict

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, has_perm
from apps.accounts.scoping import allowed_location_ids
from apps.core.middleware import ORG_PARAM
from apps.core.tenancy import require_current_organization, tenant_context

from .models import Kind, Notification, NotificationPreference
from .selectors import receivable_kinds

logger = logging.getLogger(__name__)

SUBJECT_PREFIX = "[GALBUST CMMS] "


# --- Destinatarios --------------------------------------------------------------


def target_location_id(obj) -> int | None:
    """Ubicación que decide el alcance: la del activo o la ubicación misma (OT y solicitudes)."""
    if obj.asset_id:
        return obj.asset.location_id
    return obj.location_id


def _in_scope(membership, location_id) -> bool:
    allowed = allowed_location_ids(membership)
    return allowed is None or location_id in allowed


def active_memberships(*, user_ids=None):
    """Membresías activas de la organización del contexto (con su usuario)."""
    organization = require_current_organization()
    qs = Membership.objects.select_related("user").filter(
        organization=organization, is_active=True, user__is_active=True
    )
    if user_ids is not None:
        qs = qs.filter(user_id__in=user_ids)
    return list(qs)


def managers_in_scope(location_id, memberships=None) -> list:
    """Quienes deciden OT y solicitudes (planificador, supervisor, admin) con ese sitio a la vista."""
    memberships = active_memberships() if memberships is None else memberships
    return [m for m in memberships if has_perm(m, Perm.WORK_ORDER_MANAGE) and _in_scope(m, location_id)]


def preferences_for(user_ids, kind) -> dict:
    """{user_id: (en la app, correo)}; sin preferencia guardada, ambos canales."""
    rows = NotificationPreference.objects.filter(user_id__in=user_ids, kind=kind).values_list(
        "user_id", "in_app", "email"
    )
    prefs = {user_id: (in_app, email) for user_id, in_app, email in rows}
    return {user_id: prefs.get(user_id, (True, True)) for user_id in user_ids}


# --- Entrega --------------------------------------------------------------------


def absolute_url(path: str) -> str:
    return settings.SITE_URL.rstrip("/") + path


def email_link(path: str) -> str:
    """Enlace absoluto para un correo, con la organización del contexto (`?org=`, ADR-003).

    Quien pertenece a varias organizaciones llega a la correcta: TenantMiddleware
    cambia la activa si tiene membresía ahí y quita el parámetro.
    """
    path, hash_mark, fragment = path.partition("#")
    separator = "&" if "?" in path else "?"
    organization_id = require_current_organization().pk
    return absolute_url(f"{path}{separator}{ORG_PARAM}={organization_id}{hash_mark}{fragment}")


def _send_email(*, user, subject, template, context) -> None:
    context = {
        **context,
        "user": user,
        "organization": require_current_organization(),
        "preferences_url": email_link(reverse("notification_preferences")),
    }
    try:
        send_mail(
            SUBJECT_PREFIX + subject,
            render_to_string(f"notifications/email/{template}.txt", context),
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            html_message=render_to_string(f"notifications/email/{template}.html", context),
        )
    except Exception:  # un correo que falla no revierte ni bloquea el aviso en la app
        logger.exception("No se pudo enviar la notificación por correo a %s", user.pk)


def deliver(
    *,
    kind: str,
    memberships,
    title: str,
    body: str = "",
    url: str = "",
    work_order=None,
    work_request=None,
    actor=None,
    location_id=None,
) -> list[Notification]:
    """Crea el aviso para cada membresía (según sus preferencias) y envía el correo.

    Se excluye a quien hizo la acción y a quien no tiene el objetivo en su alcance.
    """
    organization = require_current_organization()
    recipients = {
        m.user_id: m
        for m in memberships
        if m.organization_id == organization.id
        and m.is_active
        and (actor is None or m.user_id != actor.pk)
        and _in_scope(m, location_id)
    }
    if not recipients:
        return []
    prefs = preferences_for(list(recipients), kind)
    rows = [
        Notification(
            organization=organization,
            recipient_id=user_id,
            kind=kind,
            title=title[:200],
            body=body[:500],
            url=url,
            work_order=work_order,
            work_request=work_request,
            in_app=prefs[user_id][0],
        )
        for user_id in recipients
        if any(prefs[user_id])
    ]
    created = Notification.objects.bulk_create(rows)
    for user_id, membership in recipients.items():
        if prefs[user_id][1]:
            _send_email(
                user=membership.user,
                subject=title,
                template="notification",
                context={"title": title, "body": body, "url": email_link(url) if url else ""},
            )
    return created


def _on_commit(func, **kwargs) -> None:
    """Ejecuta `func` al confirmar la transacción, en la organización actual."""
    organization = require_current_organization()

    def run():
        with tenant_context(organization):
            func(**kwargs)

    transaction.on_commit(run, robust=True)


# --- Eventos --------------------------------------------------------------------


def notify_work_order_assigned(*, work_order, users, previous=(), actor=None) -> None:
    """A quien pasa a ser responsable o apoyo de la OT (no a quien ya lo era ni a quien asigna)."""
    user_ids = {user.pk for user in users if user is not None} - set(previous)
    if user_ids:
        _on_commit(
            _deliver_assignment,
            work_order_id=work_order.pk,
            user_ids=user_ids,
            actor=actor,
        )


def _deliver_assignment(*, work_order_id, user_ids, actor) -> None:
    from apps.work.models import WorkOrder

    work_order = WorkOrder.objects.select_related("asset").get(pk=work_order_id)
    deliver(
        kind=Kind.WO_ASSIGNED,
        memberships=active_memberships(user_ids=user_ids),
        title=_("Te asignaron la %(code)s") % {"code": work_order.code},
        body=work_order.title,
        url=reverse("work_order_detail", args=[work_order.pk]),
        work_order=work_order,
        actor=actor,
        location_id=target_location_id(work_order),
    )


def notify_work_order_scheduled(*, work_order, actor=None) -> None:
    """Al responsable y al apoyo de la OT: se programó (o reprogramó) para un día (REQ-05 CA2)."""
    _on_commit(_deliver_scheduled, work_order_id=work_order.pk, actor=actor)


def _deliver_scheduled(*, work_order_id, actor) -> None:
    from apps.work.models import WorkOrder, WorkOrderHelper

    work_order = WorkOrder.objects.select_related("asset").get(pk=work_order_id)
    if work_order.scheduled_for is None:
        return
    user_ids = {
        work_order.assigned_to_id,
        *WorkOrderHelper.objects.filter(work_order=work_order).values_list("user_id", flat=True),
    } - {None}
    if not user_ids:
        return
    deliver(
        kind=Kind.WO_SCHEDULED,
        memberships=active_memberships(user_ids=user_ids),
        title=_("%(code)s programada para el %(date)s")
        % {"code": work_order.code, "date": date_format(work_order.scheduled_for, "l j \\d\\e F")},
        body=work_order.title,
        url=reverse("work_order_detail", args=[work_order.pk]),
        work_order=work_order,
        actor=actor,
        location_id=target_location_id(work_order),
    )


def notify_request_created(*, work_request, actor=None) -> None:
    """A planificadores, supervisores y administradores con el sitio de la solicitud a la vista."""
    _on_commit(_deliver_request_created, work_request_id=work_request.pk, actor=actor)


def _deliver_request_created(*, work_request_id, actor) -> None:
    from apps.work.models import WorkRequest

    work_request = WorkRequest.objects.select_related("asset").get(pk=work_request_id)
    location_id = target_location_id(work_request)
    deliver(
        kind=Kind.REQUEST_NEW,
        memberships=managers_in_scope(location_id),
        title=_("Nueva solicitud #%(number)s") % {"number": work_request.number},
        body=work_request.description,
        url=reverse("request_list"),
        work_request=work_request,
        actor=actor,
        location_id=location_id,
    )


def notify_request_resolved(*, work_request, actor=None) -> None:
    """A quien pidió el trabajo: su solicitud se rechazó, se fusionó o su OT se completó."""
    if work_request.requested_by_id:
        _on_commit(_deliver_request_resolved, work_request_id=work_request.pk, actor=actor)


def _deliver_request_resolved(*, work_request_id, actor) -> None:
    from apps.work.models import WorkRequest

    work_request = WorkRequest.objects.select_related("asset", "work_order").get(pk=work_request_id)
    number = {"number": work_request.number}
    if work_request.status == WorkRequest.Status.REJECTED:
        title = _("Tu solicitud #%(number)s fue rechazada") % number
        body = work_request.rejection_reason
    elif work_request.status == WorkRequest.Status.MERGED:
        title = _("Tu solicitud #%(number)s se unió a otra por el mismo problema") % number
        body = work_request.description
    else:
        title = _("Tu solicitud #%(number)s fue atendida") % number
        body = work_request.work_order.completion_notes if work_request.work_order else ""
    work_order = work_request.work_order
    deliver(
        kind=Kind.REQUEST_RESOLVED,
        memberships=active_memberships(user_ids=[work_request.requested_by_id]),
        title=title,
        body=body or work_request.description,
        url=reverse("work_order_detail", args=[work_order.pk]) if work_order else "",
        work_order=work_order,
        work_request=work_request,
        actor=actor,
        location_id=target_location_id(work_request),
    )


# --- Vencimientos (resumen diario) ------------------------------------------------

DUE_SOON_DAYS = 1  # "por vencer": vence hoy o mañana (dentro de ~24 h)


def send_due_notifications(*, today: datetime.date | None = None) -> int:
    """Avisa las OT abiertas por vencer y vencidas de la organización del contexto.

    Una notificación por OT, persona y día (`dedupe_key`): correr el envío dos
    veces el mismo día no repite nada. El correo es un resumen por persona.
    Destinatarios: responsable y apoyo; si la OT no tiene a nadie, quienes la
    gestionan en ese sitio. Devuelve cuántas notificaciones creó.
    """
    from apps.work.models import WorkOrder
    from apps.work.selectors import OPEN_STATUSES

    organization = require_current_organization()
    if today is None:
        today = timezone.localdate(timezone=zoneinfo.ZoneInfo(organization.timezone))
    work_orders = list(
        WorkOrder.objects.filter(
            status__in=OPEN_STATUSES,
            due_on__isnull=False,
            due_on__lte=today + datetime.timedelta(days=DUE_SOON_DAYS),
        )
        .select_related("asset")
        .prefetch_related("helpers")
        .order_by("due_on", "number")
    )
    if not work_orders:
        return 0
    memberships = active_memberships()
    by_user = {m.user_id: m for m in memberships}
    prefs = {kind: preferences_for(list(by_user), kind) for kind in (Kind.WO_DUE_SOON, Kind.WO_OVERDUE)}
    already = set(
        Notification.objects.filter(
            kind__in=[Kind.WO_DUE_SOON, Kind.WO_OVERDUE], dedupe_key__endswith=f":{today.isoformat()}"
        ).values_list("recipient_id", "dedupe_key")
    )

    rows, digests = [], defaultdict(list)
    for work_order in work_orders:
        kind = Kind.WO_OVERDUE if work_order.due_on < today else Kind.WO_DUE_SOON
        location_id = target_location_id(work_order)
        assignee_ids = {work_order.assigned_to_id, *(user.pk for user in work_order.helpers.all())} - {None}
        if assignee_ids:
            recipients = [by_user[pk] for pk in assignee_ids if pk in by_user]
        else:
            recipients = managers_in_scope(location_id, memberships)
        key = f"{kind}:{work_order.pk}:{today.isoformat()}"
        for membership in recipients:
            in_app, email = prefs[kind][membership.user_id]
            if not (in_app or email) or not _in_scope(membership, location_id):
                continue
            if (membership.user_id, key) in already:
                continue
            rows.append(
                Notification(
                    organization=organization,
                    recipient_id=membership.user_id,
                    kind=kind,
                    title=_due_title(kind, work_order),
                    body=work_order.title,
                    url=reverse("work_order_detail", args=[work_order.pk]),
                    work_order=work_order,
                    in_app=in_app,
                    dedupe_key=key,
                )
            )
            if email:
                digests[membership.user_id].append((kind, work_order))
    # ignore_conflicts: dos ejecuciones simultáneas (cron y worker) tampoco duplican.
    Notification.objects.bulk_create(rows, ignore_conflicts=True)
    for user_id, items in digests.items():
        _send_email(
            user=by_user[user_id].user,
            subject=_("OT por vencer y vencidas (%(count)s)") % {"count": len(items)},
            template="digest",
            context={
                "items": [
                    {
                        "overdue": kind == Kind.WO_OVERDUE,
                        "code": work_order.code,
                        "title": work_order.title,
                        "due_on": work_order.due_on,
                        "url": email_link(reverse("work_order_detail", args=[work_order.pk])),
                    }
                    for kind, work_order in items
                ],
            },
        )
    return len(rows)


def _due_title(kind, work_order) -> str:
    if kind == Kind.WO_OVERDUE:
        return _("La %(code)s está vencida (vencía el %(date)s)") % {
            "code": work_order.code,
            "date": work_order.due_on.strftime("%d-%m-%Y"),
        }
    return _("La %(code)s vence el %(date)s") % {
        "code": work_order.code,
        "date": work_order.due_on.strftime("%d-%m-%Y"),
    }


# --- Bandeja y preferencias -------------------------------------------------------


def mark_read(*, membership, notification_id) -> Notification:
    """Marca leída una notificación propia (lanza DoesNotExist si no es de la persona)."""
    notification = Notification.objects.get(pk=notification_id, recipient=membership.user)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])
    return notification


def mark_all_read(*, membership) -> int:
    now = timezone.now()
    return Notification.objects.filter(recipient=membership.user, read_at__isnull=True).update(
        read_at=now, updated_at=now
    )


@transaction.atomic
def save_preferences(*, membership, values: dict) -> None:
    """Guarda las preferencias propias: {kind: (en la app, correo)}.

    Solo los tipos que el rol puede recibir; las de los demás tipos quedan como estaban.
    """
    allowed = set(receivable_kinds(membership=membership))
    for kind, (in_app, email) in values.items():
        if kind not in allowed:
            continue
        NotificationPreference.objects.update_or_create(
            organization=membership.organization,
            user=membership.user,
            kind=kind,
            defaults={"in_app": bool(in_app), "email": bool(email)},
        )
