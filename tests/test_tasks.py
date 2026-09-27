"""Tareas en segundo plano (Procrastinate): se prueban las funciones directamente.

Procrastinate no soporta SQLite; estas pruebas no tocan la cola (se reemplaza
`configure`/`defer`), así que corren igual en local y en CI (PostgreSQL).
"""

from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings
from procrastinate.contrib.django import app

from apps.core import tasks as core_tasks
from apps.core.mail import QueuedEmailBackend, deserialize_message, serialize_message
from apps.core.tenancy import tenant_context
from apps.maintenance import tasks as maintenance_tasks
from apps.maintenance.services import assign_plan


def _periodic_by_task():
    return {p.task.name: p for p in app.periodic_registry.periodic_tasks.values()}


def test_periodic_tasks_are_registered():
    periodic = _periodic_by_task()
    assert periodic["maintenance.enqueue_preventive_generation"].cron == "0 * * * *"  # PM-05 CA1
    assert periodic["core.backup_database"].cron == "0 7 * * *"
    assert "core.remove_old_jobs" in periodic


def test_task_skips_inactive_organization(tenant_a, today):
    with tenant_context(tenant_a.org):
        assign_plan(plan=tenant_a.plan, asset=tenant_a.asset, starts_on=today)
    tenant_a.org.is_active = False
    tenant_a.org.save(update_fields=["is_active"])
    assert maintenance_tasks.generate_preventive_work_orders(organization_id=str(tenant_a.org.pk)) == 0


def test_hourly_task_enqueues_one_job_per_active_organization(tenant_a, tenant_b, monkeypatch):
    tenant_b.org.is_active = False
    tenant_b.org.save(update_fields=["is_active"])
    deferred = []

    class Deferrer:
        def __init__(self, lock):
            self.lock = lock

        def defer(self, **kwargs):
            deferred.append((self.lock, kwargs))

    monkeypatch.setattr(
        maintenance_tasks.generate_preventive_work_orders,
        "configure",
        lambda queueing_lock: Deferrer(queueing_lock),
    )
    assert maintenance_tasks.enqueue_preventive_generation(timestamp=0) == 1
    org_id = str(tenant_a.org.pk)
    assert deferred == [(f"preventive-{org_id}", {"organization_id": org_id})]


@override_settings(BACKUPS_ENABLED=False)
def test_daily_backup_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(core_tasks.backups, "run_backup", lambda: pytest_fail("no debe respaldar"))
    assert core_tasks.backup_database(timestamp=0) is None


@override_settings(BACKUPS_ENABLED=True)
def test_daily_backup_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(core_tasks.backups, "run_backup", lambda: "backups/x.dump")
    assert core_tasks.backup_database(timestamp=0) == "backups/x.dump"


def pytest_fail(message):
    raise AssertionError(message)


def _message():
    message = EmailMultiAlternatives(
        subject="Invitación", body="Hola", from_email="a@galbust.cl", to=["b@empresa.cl"], cc=["c@empresa.cl"]
    )
    message.attach_alternative("<p>Hola</p>", "text/html")
    return message


def test_email_roundtrips_through_serialization():
    rebuilt = deserialize_message(serialize_message(_message()))
    assert (rebuilt.subject, rebuilt.body, rebuilt.to, rebuilt.cc) == (
        "Invitación",
        "Hola",
        ["b@empresa.cl"],
        ["c@empresa.cl"],
    )
    assert rebuilt.alternatives[0][1] == "text/html"


@override_settings(EMAIL_DELIVERY_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_send_email_task_delivers_with_real_backend():
    assert core_tasks.send_email(message=serialize_message(_message())) == 1
    assert mail.outbox[-1].subject == "Invitación"


@override_settings(EMAIL_DELIVERY_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_queued_backend_defers_on_commit(db, monkeypatch, django_capture_on_commit_callbacks):
    deferred = []
    monkeypatch.setattr(core_tasks.send_email, "defer", lambda **kwargs: deferred.append(kwargs))
    with django_capture_on_commit_callbacks(execute=True):
        assert QueuedEmailBackend().send_messages([_message()]) == 1
    assert deferred[0]["message"]["to"] == ["b@empresa.cl"]
    assert mail.outbox == []
