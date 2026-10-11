"""Repuestos del catálogo en planes y reserva automática (INV-10 CA1, INV-11) con valores conocidos."""

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import load_workbook

from apps.audit.models import AuditEvent
from apps.inventory import services as inventory
from apps.inventory.from_plans import create_parts_from_plan_resources, plan_part_proposals
from apps.inventory.models import Part, Reservation, StockItem
from apps.maintenance import plan_excel
from apps.maintenance import services as plans
from apps.maintenance.models import ImmutableVersionError, PlanResource, PlanVersion
from apps.organizations.models import Plan
from apps.work.models import WorkOrder
from tests.conftest import login

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
D = Decimal
TODAY = dt.date(2026, 9, 24)
PART = PlanResource.Kind.PART


@pytest.fixture
def filter_part(in_a):
    return inventory.create_part(membership=in_a.admin, code="FIL-01", name="Filtro de aceite", unit="un")


def _plan_with_resources(code, resources, **kwargs):
    return plans.create_plan(
        code=code,
        name=f"Plan {code}",
        calendar_interval=30,
        items=[{"text": "Revisar", "item_type": "check"}],
        resources=resources,
        **kwargs,
    )


def _published_resource(plan):
    return plan.current_version.resources.get()


# --- Borrador: enlazar, llenar descripción y unidad (INV-11 CA1) ------------------------------


def test_draft_resource_with_part_fills_description_and_unit(in_a, filter_part):
    draft = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    resource = plans.add_draft_resource(
        membership=in_a.admin, version=draft, kind=PART, description=" ", quantity=D(2), part=filter_part.pk
    )
    assert (resource.description, resource.unit, resource.part_id) == (
        "Filtro de aceite",
        "un",
        filter_part.pk,
    )
    # La descripción sigue siendo editable y no se pisa con el nombre.
    plans.update_draft_resource(
        membership=in_a.admin,
        resource=resource,
        kind=PART,
        description="Filtro aceite motor",
        quantity=D(2),
        unit="pza",
        part=filter_part,
    )
    resource.refresh_from_db()
    assert (resource.description, resource.unit, resource.part_id) == (
        "Filtro aceite motor",
        "pza",
        filter_part.pk,
    )
    # En borrador el enlace se puede quitar.
    plans.update_draft_resource(
        membership=in_a.admin, resource=resource, kind=PART, description="Filtro", quantity=D(2), part=None
    )
    resource.refresh_from_db()
    assert resource.part_id is None
    assert AuditEvent.objects.filter(target_id=str(resource.pk), action=AuditEvent.Action.UPDATE).count() == 2


def test_draft_resource_rejects_tool_with_part_inactive_part_and_empty_description(in_a, filter_part):
    draft = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    with pytest.raises(ValidationError):
        plans.add_draft_resource(
            membership=in_a.admin,
            version=draft,
            kind="tool",
            description="Llave",
            quantity=1,
            part=filter_part,
        )
    with pytest.raises(ValidationError):
        plans.add_draft_resource(membership=in_a.admin, version=draft, kind=PART, description="", quantity=1)
    linked = plans.add_draft_resource(
        membership=in_a.admin, version=draft, kind=PART, description="", quantity=1, part=filter_part
    )
    inventory.set_part_active(membership=in_a.admin, part=filter_part, is_active=False)
    with pytest.raises(ValidationError):
        plans.add_draft_resource(
            membership=in_a.admin, version=draft, kind=PART, description="x", quantity=1, part=filter_part
        )
    # El recurso ya enlazado conserva su repuesto desactivado al editarse.
    plans.update_draft_resource(
        membership=in_a.admin, resource=linked, kind=PART, description="Filtro", quantity=3, part=filter_part
    )
    linked.refresh_from_db()
    assert (linked.quantity, linked.part_id) == (D(3), filter_part.pk)


def test_start_draft_copies_part_and_publish_keeps_it(in_a, filter_part):
    draft = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    plans.add_draft_resource(
        membership=in_a.admin, version=draft, kind=PART, description="", quantity=1, part=filter_part
    )
    plans.publish_draft(membership=in_a.admin, version=draft)
    in_a.plan.refresh_from_db()
    assert _published_resource(in_a.plan).part_id == filter_part.pk
    second = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    assert second.resources.get().part_id == filter_part.pk


# --- Versión publicada: enlace único (INV-11 CA2, excepción al ADR-016) -------------------------


def test_published_resource_links_once_and_stays_immutable(in_a, filter_part):
    plan = _plan_with_resources(
        "P1", [{"kind": PART, "description": "filtro", "quantity": D(2), "unit": "u"}]
    )
    resource = _published_resource(plan)
    other = inventory.create_part(membership=in_a.admin, code="FIL-02", name="Otro filtro")

    plans.link_published_resource(membership=in_a.admin, resource=resource, part=filter_part)

    resource.refresh_from_db()
    # Solo cambia el enlace: lo que el plan pide queda igual.
    assert (resource.part_id, resource.description, resource.quantity, resource.unit) == (
        filter_part.pk,
        "filtro",
        D(2),
        "u",
    )
    event = AuditEvent.objects.get(target_id=str(resource.pk), action=AuditEvent.Action.UPDATE)
    assert event.changes["part"] == [None, str(filter_part.pk)] and event.changes["published"] is True
    # Nunca se cambia ni se quita un enlace, ni se edita otra cosa de la versión publicada.
    with pytest.raises(ValidationError):
        plans.link_published_resource(membership=in_a.admin, resource=resource, part=other)
    resource.description = "cambiado"
    with pytest.raises(ImmutableVersionError):
        resource.save()
    with pytest.raises(ImmutableVersionError):
        PlanResource.objects.get(pk=resource.pk).delete()
    with pytest.raises(plans.PlanVersionError):
        plans.update_draft_resource(
            membership=in_a.admin, resource=resource, kind=PART, description="x", quantity=1, part=None
        )
    resource.refresh_from_db()
    assert (resource.part_id, resource.description) == (filter_part.pk, "filtro")


def test_tool_resource_cannot_be_linked(in_a, filter_part):
    plan = _plan_with_resources(
        "P1", [{"kind": "tool", "description": "Llave", "quantity": D(1), "unit": ""}]
    )
    with pytest.raises(ValidationError):
        plans.link_published_resource(
            membership=in_a.admin, resource=_published_resource(plan), part=filter_part
        )


# --- Reserva automática al generar (INV-10 CA1) -------------------------------------------------


def _absorbing_plans(in_a, filter_part, oil):
    big = plans.create_plan(
        code="SEM",
        name="Semestral",
        calendar_interval=6,
        calendar_unit="months",
        items=[{"text": "Cambiar aceite", "item_type": "check"}],
        resources=[
            {"kind": PART, "description": "Filtro", "quantity": D(1), "unit": "un", "part": filter_part},
            {"kind": PART, "description": "Aceite", "quantity": D("4.5"), "unit": "l", "part": oil},
            {"kind": PART, "description": "Sin catálogo", "quantity": D(1), "unit": ""},
            {"kind": "tool", "description": "Llave", "quantity": D(1), "unit": ""},
        ],
    )
    small = plans.create_plan(
        code="MEN",
        name="Mensual",
        calendar_interval=1,
        calendar_unit="months",
        items=[{"text": "Limpiar", "item_type": "check"}],
        resources=[
            {"kind": PART, "description": "Filtro", "quantity": D(2), "unit": "un", "part": filter_part}
        ],
    )
    plans.add_plan_absorption(membership=in_a.admin, plan=big, absorbed_plan=small)
    plans.assign_plan(plan=big, asset=in_a.asset, starts_on=dt.date(2026, 10, 1))
    plans.assign_plan(plan=small, asset=in_a.asset, starts_on=dt.date(2026, 10, 5))
    return big, small


def test_generation_reserves_plan_parts_summing_absorbed_plans(in_a, filter_part):
    oil = inventory.create_part(membership=in_a.admin, code="ACE", name="Aceite", unit="l")
    inventory.receive_stock(membership=in_a.admin, part=filter_part, quantity=10, unit_cost="5")
    big, _small = _absorbing_plans(in_a, filter_part, oil)

    [wo] = plans.generate_due_work_orders(today=TODAY, horizon_days=14)

    reservations = {r.part_id: r for r in Reservation.objects.filter(work_order=wo)}
    assert set(reservations) == {filter_part.pk, oil.pk}
    assert reservations[filter_part.pk].quantity == D(3)  # 1 del mayor + 2 del absorbido
    assert reservations[oil.pk].quantity == D("4.5")
    assert {r.source for r in reservations.values()} == {"plan"}
    assert reservations[filter_part.pk].plan_resource.version_id == big.current_version_id
    assert reservations[filter_part.pk].created_by is None
    item = StockItem.objects.get(part=filter_part)
    assert (item.on_hand, item.reserved) == (D(10), D(3))
    # Sin stock no bloquea: el aceite queda reservado con existencia 0 ("Faltan N").
    assert StockItem.objects.get(part=oil).reserved == D("4.5")
    assert wo.status == WorkOrder.Status.PLANNED


def test_generation_without_inventory_module_does_not_reserve(in_a, filter_part):
    in_a.org.plan = Plan.objects.create(code="basico", name="Básico", features=["assets", "preventive"])
    in_a.org.save(update_fields=["plan"])
    oil = inventory.create_part(membership=in_a.admin, code="ACE", name="Aceite", unit="l")
    _absorbing_plans(in_a, filter_part, oil)

    [wo] = plans.generate_due_work_orders(today=TODAY, horizon_days=14)

    assert not Reservation.objects.filter(work_order=wo).exists()


def test_generation_skips_inactive_parts_and_never_blocks(in_a, filter_part):
    plan = _plan_with_resources(
        "P1", [{"kind": PART, "description": "Filtro", "quantity": D(1), "unit": "", "part": filter_part}]
    )
    inventory.set_part_active(membership=in_a.admin, part=filter_part, is_active=False)
    plans.assign_plan(plan=plan, asset=in_a.asset, starts_on=TODAY)

    [wo] = plans.generate_due_work_orders(today=TODAY, horizon_days=14)

    assert not Reservation.objects.filter(work_order=wo).exists()


def test_generation_survives_a_failed_reservation(in_a, filter_part, monkeypatch):
    plan = _plan_with_resources(
        "P1", [{"kind": PART, "description": "Filtro", "quantity": D(1), "unit": "", "part": filter_part}]
    )
    plans.assign_plan(plan=plan, asset=in_a.asset, starts_on=TODAY)

    def broken(**kwargs):
        raise inventory.InventoryError("bodega desactivada")

    monkeypatch.setattr(inventory, "reserve_for_work_order", broken)
    [wo] = plans.generate_due_work_orders(today=TODAY, horizon_days=14)

    assert WorkOrder.objects.filter(pk=wo.pk).exists()
    assert not Reservation.objects.filter(work_order=wo).exists()


# --- Crear catálogo desde los planes (INV-11 CA3) ---------------------------------------------


def _plans_with_free_text(in_a):
    one = _plan_with_resources(
        "P1",
        [
            {"kind": PART, "description": "Filtro de aceite", "quantity": D(1), "unit": "un"},
            {"kind": PART, "description": "Correa  B-42", "quantity": D(2), "unit": "pza"},
            {"kind": "tool", "description": "Llave 10 mm", "quantity": D(1), "unit": ""},
        ],
    )
    two = _plan_with_resources(
        "P2",
        [
            {"kind": PART, "description": "  filtro DE   aceite ", "quantity": D(1), "unit": "un"},
            {"kind": PART, "description": "correa b-42", "quantity": D(1), "unit": "pza"},
        ],
    )
    return one, two


def test_proposals_group_descriptions_and_propose_free_codes(in_a):
    one, two = _plans_with_free_text(in_a)
    inventory.create_part(membership=in_a.admin, code="REP-0001", name="Otro repuesto")
    draft = plans.create_draft(membership=in_a.admin, plan=one)  # borrador copia los recursos

    proposals = plan_part_proposals(membership=in_a.admin)

    assert [(p.key, p.description, p.code, p.unit, p.resource_count, p.plans) for p in proposals] == [
        ("correa b-42", "Correa B-42", "REP-0002", "pza", 3, ["P1", "P2"]),
        ("filtro de aceite", "Filtro de aceite", "REP-0003", "un", 3, ["P1", "P2"]),
    ]
    assert draft.resources.count() == 3


def test_create_from_plans_links_published_and_draft_resources_and_is_idempotent(in_a):
    one, two = _plans_with_free_text(in_a)
    draft = plans.create_draft(membership=in_a.admin, plan=one)

    assert create_parts_from_plan_resources(membership=in_a.admin) == (2, 6)

    correa = Part.objects.get(code="REP-0001")
    filtro = Part.objects.get(code="REP-0002")
    assert (correa.name, correa.unit, filtro.name, filtro.unit) == (
        "Correa B-42",
        "pza",
        "Filtro de aceite",
        "un",
    )
    linked = PlanResource.objects.filter(kind=PART).exclude(version__status=PlanVersion.Status.SUPERSEDED)
    assert {r.part_id for r in linked} == {correa.pk, filtro.pk}
    # Las versiones publicadas no cambian salvo el enlace.
    assert sorted(two.current_version.resources.values_list("description", flat=True)) == [
        "  filtro DE   aceite ",
        "correa b-42",
    ]
    assert draft.resources.filter(part__isnull=True).count() == 1  # la herramienta
    # Repetir no duplica.
    assert create_parts_from_plan_resources(membership=in_a.admin) == (0, 0)
    assert Part.objects.count() == 2


def test_create_from_plans_reuses_existing_part_with_same_name_and_filters(in_a):
    _plans_with_free_text(in_a)
    existing = inventory.create_part(membership=in_a.admin, code="FIL-9", name="FILTRO de aceite")

    assert create_parts_from_plan_resources(membership=in_a.admin, descriptions=["Filtro de Aceite"]) == (
        0,
        2,
    )

    assert set(PlanResource.objects.filter(part=existing).values_list("version__plan__code", flat=True)) == {
        "P1",
        "P2",
    }
    assert PlanResource.objects.filter(kind=PART, part__isnull=True).count() == 2  # las correas


def test_from_plans_screen_previews_and_confirms(client, in_a):
    _plans_with_free_text(in_a)
    login(client, in_a.admin)

    response = client.get(reverse("inventory_from_plans"))
    assert response.status_code == 200
    assert "REP-0001" in response.content.decode() and not Part.objects.exists()

    response = client.post(reverse("inventory_from_plans_confirm"))
    assert response.status_code == 302
    assert Part.objects.count() == 2
    assert "Todos los repuestos" in client.get(reverse("inventory_from_plans")).content.decode()


# --- Pantallas del plan --------------------------------------------------------------------------


def test_draft_screen_adds_resource_with_part_and_search_finds_it(client, in_a, filter_part):
    draft = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    login(client, in_a.admin)
    url = reverse("plan_draft", args=[in_a.plan.pk])
    assert "part_q" in client.get(url).content.decode()

    response = client.post(
        url,
        {
            "action": "resource",
            "resource-kind": PART,
            "resource-quantity": "2",
            "resource-part": filter_part.pk,
        },
    )

    assert response.status_code == 302
    resource = draft.resources.get()
    assert (resource.description, resource.part_id) == ("Filtro de aceite", filter_part.pk)
    found = client.get(reverse("inventory_plan_part_search"), {"part_q": "fil", "name": "resource-part"})
    assert f'value="{filter_part.pk}"' in found.content.decode()
    assert (
        "FIL-01"
        not in client.get(
            reverse("inventory_plan_part_search"), {"part_q": "zzz", "name": "resource-part"}
        ).content.decode()
    )


def test_published_link_screen(client, in_a, filter_part):
    plan = _plan_with_resources("P1", [{"kind": PART, "description": "filtro", "quantity": D(1), "unit": ""}])
    resource = _published_resource(plan)
    login(client, in_a.admin)
    detail = client.get(reverse("plan_detail", args=[plan.pk])).content.decode()
    link_url = reverse("plan_resource_link", args=[plan.pk, resource.pk])
    assert link_url in detail

    assert client.post(link_url, {"part": filter_part.pk}).status_code == 302
    resource.refresh_from_db()
    assert resource.part_id == filter_part.pk
    # Ya enlazado: la pantalla deja de existir para ese recurso.
    assert client.get(link_url).status_code == 404
    assert link_url not in client.get(reverse("plan_detail", args=[plan.pk])).content.decode()


def test_plan_screens_hide_catalog_without_inventory_module(client, in_a, filter_part):
    in_a.org.plan = Plan.objects.create(code="basico", name="Básico", features=["assets", "preventive"])
    in_a.org.save(update_fields=["plan"])
    plan = _plan_with_resources("P1", [{"kind": PART, "description": "filtro", "quantity": D(1), "unit": ""}])
    resource = _published_resource(plan)
    draft = plans.create_draft(membership=in_a.admin, plan=plan)
    login(client, in_a.admin)

    assert "part_q" not in client.get(reverse("plan_draft", args=[plan.pk])).content.decode()
    detail = client.get(reverse("plan_detail", args=[plan.pk])).content.decode()
    assert "Enlazar al catálogo" not in detail
    assert client.get(reverse("plan_resource_link", args=[plan.pk, resource.pk])).status_code == 404
    assert client.get(reverse("inventory_from_plans")).status_code == 403
    assert client.get(reverse("inventory_plan_part_search")).status_code == 403
    # Un repuesto enviado a mano sin el módulo se ignora.
    client.post(
        reverse("plan_draft", args=[plan.pk]),
        {"action": "resource", "resource-kind": PART, "resource-description": "x", "resource-quantity": "1",
         "resource-part": filter_part.pk},
    )  # fmt: skip
    assert draft.resources.get(description="x").part_id is None


# --- Excel de planes -------------------------------------------------------------------------------


def test_plan_excel_exports_and_imports_part_code(in_a, filter_part):
    _plan_with_resources(
        "P1", [{"kind": PART, "description": "Filtro", "quantity": D(1), "unit": "un", "part": filter_part}]
    )
    content = plan_excel.build_plans_workbook(membership=in_a.admin)
    sheet = load_workbook(io.BytesIO(content))["Recursos"]
    rows = list(sheet.values)
    assert rows[0][-1] == "Código de repuesto" and rows[1][-1] == "FIL-01"

    # Reimportar el mismo archivo no cambia nada (el enlace forma parte del contenido).
    raw = plan_excel.parse_plan_workbook(SimpleUploadedFile("p.xlsx", content))
    preview = plan_excel.build_plan_preview(raw, membership=in_a.admin)
    assert [r.action for r in preview.rows if r.code == "P1"] == ["unchanged"]

    # Un plan nuevo con código de repuesto queda enlazado; un código desconocido es error de fila.
    raw["plans"].append({**raw["plans"][-1], "code": "P9", "name": "Nuevo", "_row": 99})
    raw["resources"] += [
        {"plan": "P9", "kind": "Repuesto", "description": "Filtro", "part": "fil-01", "_row": 50},
    ]
    raw["items"].append({**raw["items"][0], "plan": "P9", "_row": 60})
    preview = plan_excel.build_plan_preview(raw, membership=in_a.admin)
    assert [r.action for r in preview.rows if r.code == "P9"] == ["create"]
    plan_excel.apply_plan_import(preview.to_session(), membership=in_a.admin)
    created = plans.MaintenancePlan.objects.get(code="P9")
    assert created.current_version.resources.get().part_id == filter_part.pk

    raw["plans"][-1]["code"], raw["resources"][-1]["plan"], raw["items"][-1]["plan"] = "P10", "P10", "P10"
    raw["resources"][-1]["part"] = "NO-EXISTE"
    preview = plan_excel.build_plan_preview(raw, membership=in_a.admin)
    [row] = [r for r in preview.rows if r.code == "P10"]
    assert row.action == "error" and "NO-EXISTE" in " ".join(row.errors)
