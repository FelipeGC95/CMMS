import pytest
from django.core.management import call_command

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization
from apps.work.models import WorkOrder
from apps.work.selectors import OPEN_STATUSES

pytestmark = pytest.mark.django_db

ADMIN_PERMS = {"maintenance.change_planversion", "audit.view_auditevent"}


def _admin():
    # Recarga desde la base: has_perm guarda en caché los permisos de la instancia.
    return User.objects.get(email="jefe@demo.cl")


def test_demo_admin_can_manage_versions_and_read_audit():
    call_command("seed_demo", verbosity=0)

    admin = _admin()
    assert admin.has_perms(ADMIN_PERMS)
    assert not admin.has_perm("audit.change_auditevent")


def test_rerun_restores_admin_permissions_on_existing_data():
    # En Render la demo ya existe y seed_demo corre en cada arranque.
    call_command("seed_demo", verbosity=0)
    _admin().user_permissions.clear()

    call_command("seed_demo", verbosity=0)

    assert _admin().has_perms(ADMIN_PERMS)


def _open_work_orders():
    with tenant_context(Organization.objects.get(slug="alimentos-del-sur")):
        return list(WorkOrder.objects.filter(status__in=OPEN_STATUSES).select_related("assigned_to"))


def test_demo_technicians_have_open_work_orders():
    # "Mis OT" es la vista por defecto del técnico: no debe quedar vacía en la demo.
    call_command("seed_demo", verbosity=0)

    open_orders = _open_work_orders()
    assert open_orders
    assert all(wo.assigned_to is not None for wo in open_orders)
    assert {wo.assigned_to.email for wo in open_orders} == {"tecnico@demo.cl", "tecnico2@demo.cl"}


def test_rerun_assigns_open_work_orders_left_without_technician():
    # La base de Render ya existe: el nuevo arranque corrige las OT sin responsable.
    call_command("seed_demo", verbosity=0)
    with tenant_context(Organization.objects.get(slug="alimentos-del-sur")):
        WorkOrder.objects.filter(status__in=OPEN_STATUSES).update(assigned_to=None)

    call_command("seed_demo", verbosity=0)

    assert all(wo.assigned_to is not None for wo in _open_work_orders())


def test_demo_has_an_open_corrective_from_a_failure_report_with_checklist():
    # MVP-0 "Hecho cuando": aviso por QR → OT correctiva → checklist en el celular.
    call_command("seed_demo", verbosity=0)

    with tenant_context(Organization.objects.get(slug="alimentos-del-sur")):
        candidates = [
            wo
            for wo in _open_work_orders()
            if wo.work_type == WorkOrder.Type.CORRECTIVE and hasattr(wo, "request")
        ]
        with_checklist = [wo for wo in candidates if wo.checklist.filter(required=True).exists()]
        assert with_checklist
        wo = with_checklist[0]
        assert wo.assigned_to.email == "tecnico@demo.cl"
        assert wo.checklist.filter(item_type="measurement").exists()


def test_demo_has_inventory_with_history_and_alerts():
    # INV (P7): bodega principal + una por sitio, catálogo, planes enlazados, reservas, consumos
    # históricos (para los indicadores), un repuesto bajo mínimo y uno en negativo.
    from apps.accounts.models import Membership
    from apps.inventory.models import Part, Reservation, Warehouse
    from apps.inventory.selectors import inventory_insights, stock_counters
    from apps.inventory.services import reconcile_stock
    from apps.maintenance.models import PlanResource
    from apps.organizations.models import Location

    call_command("seed_demo", verbosity=0)

    org = Organization.objects.get(slug="alimentos-del-sur")
    admin = Membership.objects.get(organization=org, user__email="jefe@demo.cl")
    with tenant_context(org):
        sites = set(Location.objects.filter(kind=Location.Kind.SITE).values_list("pk", flat=True))
        warehouses = list(Warehouse.objects.all())
        assert sum(w.is_default for w in warehouses) == 1
        assert sites <= {w.location_id for w in warehouses}
        assert Part.objects.count() >= 30
        assert PlanResource.objects.filter(
            part__isnull=False, version__plan__current_version__isnull=False
        ).exists()
        assert Reservation.objects.filter(status=Reservation.Status.ACTIVE).exists()
        counters = stock_counters(membership=admin)
        assert counters["below_min"] >= 1
        assert counters["negative"] >= 1
        insights = inventory_insights(membership=admin)
        assert insights.total_value > 0
        assert insights.consumption > 0
        assert insights.stale_count >= 1
        assert reconcile_stock() == []


def test_demo_has_condition_variables_rules_and_alert_episodes():
    # CBM-10 CA1: variables, reglas por clase y por activo, 90 días de historia y episodios
    # creados por la evaluación real (estados coherentes con la última lectura).
    from apps.accounts.models import Membership
    from apps.assets.models import VARIABLE_KINDS, Asset, Meter
    from apps.condition.models import ConditionAlert, ConditionRule, ConditionState
    from apps.condition.selectors import VariableStatus, asset_condition
    from apps.maintenance.models import PlanTaskItem

    call_command("seed_demo", verbosity=0)

    org = Organization.objects.get(slug="alimentos-del-sur")
    admin = Membership.objects.get(organization=org, user__email="jefe@demo.cl")
    with tenant_context(org):
        variables = list(Meter.objects.filter(kind__in=VARIABLE_KINDS))
        assert len(variables) >= 10
        assert {m.kind for m in variables} >= {"temperature", "vibration", "pressure", "current"}
        assert {m.organization_id for m in variables} == {org.pk}
        rules = list(ConditionRule.objects.all())
        assert sum(1 for r in rules if r.asset_class_id and r.is_active) >= 3
        assert any(r.meter_id and r.is_active for r in rules)
        assert {r.operator for r in rules} == {"gt", "lt", "outside", "rate"}
        assert any(not r.is_active for r in rules)

        alerts = list(ConditionAlert.objects.select_related("work_order"))
        assert {a.status for a in alerts} == {"open", "acknowledged", "normalized", "closed"}
        assert {a.work_order.work_type for a in alerts if a.work_order_id} == {WorkOrder.Type.PREDICTIVE}
        assert any(a.status == "open" and a.work_order_id for a in alerts)
        assert any(a.status == "closed" and a.work_order.status == WorkOrder.Status.CLOSED for a in alerts)

        # Estado materializado: cada estado evaluó la última lectura de su variable y está en
        # alarma justo cuando su alerta sigue activa.
        active = {(a.rule_id, a.meter_id): a.pk for a in alerts if a.is_active}
        states = list(ConditionState.objects.select_related("meter"))
        assert states
        for state in states:
            assert state.last_reading_id == state.meter.readings.order_by("-read_at").first().pk
            assert (state.status == "alarm") == ((state.rule_id, state.meter_id) in active)
            if state.status == "alarm":
                assert state.open_alert_id == active[(state.rule_id, state.meter_id)]

        # Ficha del activo: tendencia de 90 días y todos los estados de la sección "Condición".
        rows = {
            code: asset_condition(membership=admin, asset=Asset.objects.get(code=code))
            for code in ("CMP-01", "CMP-02", "GRH-01", "GRH-02")
        }
        descarga = next(r for r in rows["CMP-01"] if r.meter.name == "Descarga")
        assert descarga.status == VariableStatus.ALARM
        assert len(descarga.points) >= 80
        assert {row.status for asset_rows in rows.values() for row in asset_rows} == {
            VariableStatus.ALARM,
            VariableStatus.NORMAL,
            VariableStatus.NO_READINGS,
            VariableStatus.INACTIVE,
            VariableStatus.NO_RULES,
        }

        # Un ítem de medición de un plan alimenta una variable (CBM-06).
        assert PlanTaskItem.objects.filter(meter_kind="temperature", meter_name="Descarga").exists()


def _demo_has_inventory_and_condition():
    from apps.condition.models import ConditionRule
    from apps.inventory.models import Part

    with tenant_context(Organization.objects.get(slug="alimentos-del-sur")):
        return Part.objects.exists() and ConditionRule.objects.exists()


# Transaccional: en PostgreSQL el flush (TRUNCATE) no corre dentro de la transacción del test.
@pytest.mark.django_db(transaction=True)
def test_reset_if_stale_recreates_demo_loaded_before_inventory_and_condition():
    # Base de Render cargada antes de inventario y condición: solo la organización demo.
    Organization.objects.create(name="Alimentos del Sur SpA", slug="alimentos-del-sur")

    call_command("seed_demo", "--reset-if-stale", verbosity=0)

    assert _demo_has_inventory_and_condition()
    assert User.objects.filter(email="jefe@demo.cl").exists()


def test_reset_if_stale_keeps_up_to_date_demo():
    call_command("seed_demo", verbosity=0)
    org_id = Organization.objects.get(slug="alimentos-del-sur").pk

    call_command("seed_demo", "--reset-if-stale", verbosity=0)

    # Sin recrear: la misma organización (un flush la habría cambiado).
    assert Organization.objects.get(slug="alimentos-del-sur").pk == org_id
    assert _demo_has_inventory_and_condition()


# --- Asistente de diagnóstico (AI-01..05, D3) ---------------------------------------------------


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    # La demo sube los manuales PDF del asistente: a una carpeta temporal, no a media/ (todo el archivo).
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _demo():
    from apps.accounts.models import Membership

    org = Organization.objects.get(slug="alimentos-del-sur")
    tech = Membership.objects.get(organization=org, user__email="tecnico@demo.cl")
    return org, tech


def test_demo_has_answered_assistant_conversations_with_valid_sources(media):
    # D3: conversaciones ya respondidas, sin llamar a la API, con citas a OT, manuales y
    # medidores del seed que pasan la misma validación que una respuesta real (AI-02 CA4).
    from apps.assistant.citations import validate_citations
    from apps.assistant.models import AssistantConversation, AssistantMessage
    from apps.assistant.services import allowed_sources, module_enabled
    from apps.attachments.models import DocumentPage
    from apps.organizations.modules import has_module

    call_command("seed_demo", verbosity=0)

    org, _tech = _demo()
    # D2: la organización demo sigue en Profesional, que incluye el módulo (y inventario y condición).
    assert org.plan.code == "pro"
    assert {"assistant", "inventory", "condition"} <= set(org.plan.features)
    assert has_module(org, "assistant")
    with tenant_context(org):
        assert module_enabled()
        conversations = list(AssistantConversation.objects.select_related("work_order"))
        assert len(conversations) == 4
        assert {"yes", "partial"} <= {c.helpful for c in conversations}
        assert {c.status for c in conversations} == {"open", "closed"}
        assert any(
            c.status == "open"
            and c.summary_status == "done"
            and c.summary
            and c.work_order
            and c.work_order.status in OPEN_STATUSES
            for c in conversations
        )

        types = set()
        replies = AssistantMessage.objects.filter(role="assistant", kind="turn").select_related(
            "conversation"
        )
        assert replies.exists()
        for message in replies:
            assert message.status == "done"
            assert message.model == "claude-opus-5-5"
            assert message.input_tokens and message.output_tokens
            allowed = allowed_sources(conversation=message.conversation)
            text, cited = validate_citations(message.text, allowed)
            assert text == message.text  # ningún marcador inventado
            assert cited == message.sources
            for source in message.sources:
                types.add(source["type"])
                if source["type"] == "manual":
                    assert DocumentPage.objects.filter(
                        attachment_id=source["attachment_id"], page_number=source["page"]
                    ).exists()
        assert types == {"work_order", "manual", "meter"}
        assert AssistantMessage.objects.filter(cache_read_tokens__gt=0).exists()
        assert AssistantMessage.objects.filter(cache_creation_tokens__gt=0).exists()


def test_demo_conversation_shows_linked_sources_and_closing_note(client, media):
    from django.urls import reverse

    from apps.assistant.models import AssistantConversation
    from apps.assistant.selectors import closing_conversation

    call_command("seed_demo", verbosity=0)

    org, tech = _demo()
    with tenant_context(org):
        conversation = AssistantConversation.objects.get(status="open", summary_status="done")
        work_order = conversation.work_order
        manual_id = next(
            s["attachment_id"]
            for m in conversation.messages.filter(role="assistant")
            for s in m.sources
            if s["type"] == "manual"
        )
        assert closing_conversation(membership=tech, work_order=work_order) == conversation
    client.force_login(tech.user)

    page = client.get(reverse("assistant_conversation", args=[conversation.pk])).content.decode()
    assert reverse("work_order_detail", args=[work_order.pk]) in page
    assert reverse("attachment", args=[manual_id]) in page
    assert "Conocimiento general" in page
    assert client.get(reverse("attachment", args=[manual_id])).status_code == 200

    # AI-04 CA1: al completar la OT aparece la nota de cierre propuesta.
    page = client.get(reverse("work_order_detail", args=[work_order.pk])).content.decode()
    assert "falta de refrigerante por fuga" in page


def test_rerun_restores_missing_demo_manual_files(media):
    # Render no tiene disco: los PDF de los manuales se pierden al dormir y el arranque los repone.
    from apps.attachments.models import Attachment, Purpose

    call_command("seed_demo", verbosity=0)
    org, _tech = _demo()
    with tenant_context(org):
        manuals = list(Attachment.objects.filter(purpose=Purpose.MANUAL))
    assert len(manuals) == 3
    for manual in manuals:
        manual.file.storage.delete(manual.file.name)

    call_command("seed_demo", verbosity=0)

    for manual in manuals:
        assert manual.file.storage.exists(manual.file.name)
        assert manual.file.storage.size(manual.file.name) == manual.size_bytes


@pytest.mark.django_db(transaction=True)
def test_reset_if_stale_recreates_demo_loaded_before_the_assistant(media):
    from apps.assistant.models import AssistantConversation, AssistantMessage

    call_command("seed_demo", verbosity=0)
    org, _tech = _demo()
    with tenant_context(org):
        AssistantMessage.objects.all().delete()
        AssistantConversation.objects.all().delete()

    call_command("seed_demo", "--reset-if-stale", verbosity=0)

    new_org, _tech = _demo()
    assert new_org.pk != org.pk
    with tenant_context(new_org):
        assert AssistantConversation.objects.count() == 4
