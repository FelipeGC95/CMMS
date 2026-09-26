"""Las listas no hacen consultas N+1: el número de consultas no crece con los datos."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.http import urlencode

from apps.accounts.models import Membership, MembershipLocation, User
from apps.assets.models import Asset
from apps.audit.models import AuditEvent
from apps.audit.services import record
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.models import FailureCode
from apps.work.services import create_failure_code, create_work_order, report_failure
from tests.conftest import login


def _add_rows(tenant, n):
    with tenant_context(tenant.org):
        for i in range(n):
            # Ubicaciones en 3 niveles (sitio › área › línea): mostrar la ruta no recorre `parent`.
            site = Location.objects.create(code=f"S-{Location.objects.count():03d}", name=f"Sitio {i}")
            area = Location.objects.create(
                code=f"S-{Location.objects.count():03d}", name=f"Área {i}", kind="area", parent=site
            )
            line = Location.objects.create(
                code=f"S-{Location.objects.count():03d}", name=f"Línea {i}", kind="line", parent=area
            )
            asset = Asset.objects.create(
                code=f"A-{Asset.objects.count():03d}",
                name=f"Equipo {i}",
                asset_class=tenant.asset.asset_class,
                location=line,
            )
            create_work_order(
                work_type="corrective", title=f"OT {i}", asset=asset, assigned_to=tenant.admin.user
            )
            report_failure(membership=tenant.viewer, asset=asset, description="x", asset_stopped=True)
            record(action=AuditEvent.Action.CREATE, target=asset, actor=tenant.admin.user)
            create_failure_code(
                kind=FailureCode.Kind.MODE,
                code=f"X{FailureCode.objects.count():03d}",
                name=f"Modo {i}",
                membership=tenant.admin,
            )
            # Miembros con alcance por sitio para la lista del equipo.
            user = User.objects.create_user(f"m{Asset.objects.count()}@{tenant.org.slug}.cl")
            member = Membership.objects.create(
                user=user, organization=tenant.org, role="external", restricted_to_locations=True
            )
            MembershipLocation.objects.create(membership=member, location=site)
            # OT y aviso sobre una ubicación, con apoyo (WO-01 CA2, REQ-01 CA1).
            create_work_order(
                work_type="corrective",
                title=f"OT ubicación {i}",
                location=line,
                assigned_to=tenant.admin.user,
                helpers=[tenant.tech.user],
            )
            report_failure(membership=tenant.viewer, location=line, description="gotera")


def _count(client, url):
    with CaptureQueriesContext(connection) as ctx:
        assert client.get(url).status_code == 200
    return len(ctx.captured_queries)


@pytest.mark.parametrize(
    "url_name",
    [
        "dashboard",
        "asset_list",
        "asset_create",
        "work_order_list",
        "asset_labels_pdf",
        "design_system",
        "team",
        "location_list",
        "asset_class_list",
        "audit_list",
        "request_list",
        "failure_code_list",
    ],
)
def test_list_queries_do_not_grow_with_rows(client, tenant_a, url_name):
    login(client, tenant_a.admin)
    url = reverse(url_name)
    _add_rows(tenant_a, 2)
    few = _count(client, url)
    _add_rows(tenant_a, 10)
    many = _count(client, url)
    assert many == few, f"{url_name}: {few} → {many} consultas"


def test_work_order_list_with_filters_does_not_grow_with_rows(client, tenant_a):
    """Con filtros activos (WO-05), las consultas siguen sin crecer con la cantidad de filas."""
    login(client, tenant_a.admin)
    url = reverse("work_order_list")
    params = {
        "scope": "open",
        "status": "planned",
        "type": "corrective",
        "asset_q": "a",
        "date_field": "due_on",
    }
    _add_rows(tenant_a, 2)
    few = _count(client, f"{url}?{urlencode(params)}")
    _add_rows(tenant_a, 10)
    many = _count(client, f"{url}?{urlencode(params)}")
    assert many == few


def test_asset_list_incomplete_filter_is_one_bounded_query(client, tenant_a):
    """ "Con datos técnicos incompletos" (AST-01) filtra con una subconsulta, sin consultas por fila."""
    from apps.assets.models import AssetClassAttribute

    with tenant_context(tenant_a.org):
        AssetClassAttribute.objects.create(
            asset_class=tenant_a.asset.asset_class,
            key="potencia",
            label="Potencia",
            data_type="number",
            required=True,
        )
    login(client, tenant_a.admin)
    url = f"{reverse('asset_list')}?incomplete=1"
    _add_rows(tenant_a, 2)
    few = _count(client, url)
    _add_rows(tenant_a, 10)
    many = _count(client, url)
    assert many == few, f"asset_list?incomplete=1: {few} → {many} consultas"
    # El filtro va dentro de la consulta de la lista (conteo + página), no en Python.
    with CaptureQueriesContext(connection) as ctx:
        client.get(url)
    filtered = [q for q in ctx.captured_queries if "->>" in q["sql"] or "json_extract" in q["sql"]]
    assert 1 <= len(filtered) <= 2


def test_my_work_orders_with_helpers_do_not_grow_with_rows(client, tenant_a):
    """ "Mis OT" incluye las OT donde la persona es apoyo (WO-01 CA2) sin consultas por fila."""
    login(client, tenant_a.tech)
    url = f"{reverse('work_order_list')}?scope=mine"
    _add_rows(tenant_a, 2)
    few = _count(client, url)
    _add_rows(tenant_a, 10)
    many = _count(client, url)
    assert many == few


def test_location_work_order_detail_query_budget(client, tenant_a, django_assert_max_num_queries):
    with tenant_context(tenant_a.org):
        site = Location.objects.create(code="P1", name="Planta 1")
        area = Location.objects.create(code="P1-B", name="Bodega", kind="area", parent=site)
        wo = create_work_order(
            work_type="corrective",
            title="x",
            location=area,
            helpers=[tenant_a.tech.user, tenant_a.admin.user],
        )
    login(client, tenant_a.admin)
    with django_assert_max_num_queries(25):
        assert client.get(reverse("work_order_detail", args=[wo.pk])).status_code == 200


def _add_components(tenant, n):
    """Componentes del activo de conftest, cada uno con una falla (AST-02 CA2 / AST-03)."""
    with tenant_context(tenant.org):
        for _ in range(n):
            child = Asset.objects.create(
                code=f"C-{Asset.objects.count():03d}",
                name="Componente",
                asset_class=tenant.asset.asset_class,
                parent=tenant.asset,
                location=Location.objects.create(code=f"CL-{Asset.objects.count():03d}", name="Área"),
            )
            report_failure(membership=tenant.viewer, asset=tenant.asset, description="x", asset_stopped=True)
            report_failure(membership=tenant.viewer, asset=child, description="y", asset_stopped=False)


def test_asset_detail_queries_do_not_grow_with_components_and_failures(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("asset_detail", args=[tenant_a.asset.pk])
    _add_components(tenant_a, 2)
    few = _count(client, url)
    _add_components(tenant_a, 12)
    many = _count(client, url)
    assert many == few, f"asset_detail: {few} → {many} consultas"


def _add_component_tree(tenant, n):
    """Por cada vuelta, un sistema bajo el activo y un componente bajo ese sistema (3 niveles),
    con fallas, cambios de estado y OT cerradas: "incluir componentes" los recorre todos."""
    from apps.assets.services import change_asset_status

    with tenant_context(tenant.org):
        for _ in range(n):
            system = Asset.objects.create(
                code=f"S-{Asset.objects.count():03d}",
                name="Sistema",
                asset_class=tenant.asset.asset_class,
                parent=tenant.asset,
            )
            part = Asset.objects.create(
                code=f"P-{Asset.objects.count():03d}",
                name="Pieza",
                asset_class=tenant.asset.asset_class,
                parent=system,
            )
            report_failure(membership=tenant.viewer, asset=part, description="z", asset_stopped=True)
            change_asset_status(asset=system, status="in_maintenance", reason="Revisión")
            wo = create_work_order(work_type="corrective", title="Cerrada", asset=part, status="closed")
            assert wo.status == "closed"


def test_asset_detail_with_components_queries_do_not_grow(client, tenant_a, django_assert_max_num_queries):
    login(client, tenant_a.admin)
    url = f"{reverse('asset_detail', args=[tenant_a.asset.pk])}?components=1"
    _add_component_tree(tenant_a, 2)
    few = _count(client, url)
    _add_component_tree(tenant_a, 12)
    many = _count(client, url)
    assert many == few, f"asset_detail?components=1: {few} → {many} consultas"
    with django_assert_max_num_queries(40):
        assert client.get(url).status_code == 200


def _add_plans_and_open_orders(tenant, n):
    from apps.maintenance.models import ItemType, MaintenancePlan
    from apps.maintenance.services import assign_plan, create_plan

    with tenant_context(tenant.org):
        for i in range(n):
            plan = create_plan(
                code=f"PX-{MaintenancePlan.objects.count():03d}",
                name=f"Plan {i}",
                interval_days=7,
                items=[{"text": "Revisar", "item_type": ItemType.CHECK}],
            )
            assign_plan(plan=plan, asset=tenant.asset)
            create_work_order(work_type="corrective", title=f"Abierta {i}", asset=tenant.asset)


def test_asset_decommission_confirmation_queries_do_not_grow(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("asset_decommission", args=[tenant_a.asset.pk])
    _add_plans_and_open_orders(tenant_a, 2)
    few = _count(client, url)
    _add_plans_and_open_orders(tenant_a, 10)
    many = _count(client, url)
    assert many == few, f"asset_decommission: {few} → {many} consultas"


def _add_retired_plans(tenant, n):
    from apps.assets.services import change_asset_status
    from apps.maintenance.models import ItemType, MaintenancePlan, PlanAssignment
    from apps.maintenance.services import assign_plan, create_plan

    with tenant_context(tenant.org):
        for i in range(n):
            plan = create_plan(
                code=f"PR-{MaintenancePlan.objects.count():03d}",
                name=f"Plan {i}",
                interval_days=7,
                items=[{"text": "Revisar", "item_type": ItemType.CHECK}],
            )
            change_asset_status(asset=tenant.asset, status="operational", reason="x")
            assign_plan(plan=plan, asset=tenant.asset)
            Asset.objects.create(
                code=f"C-{Asset.objects.count():03d}",
                name=f"Componente {i}",
                asset_class=tenant.asset.asset_class,
                parent=tenant.asset,
            )
        PlanAssignment.objects.filter(asset=tenant.asset).update(is_active=False)
        change_asset_status(asset=tenant.asset, status="retired", reason="Baja")


def test_asset_reactivate_confirmation_queries_do_not_grow(client, tenant_a, django_assert_max_num_queries):
    login(client, tenant_a.admin)
    url = reverse("asset_reactivate", args=[tenant_a.asset.pk])
    _add_retired_plans(tenant_a, 2)
    few = _count(client, url)
    _add_retired_plans(tenant_a, 10)
    many = _count(client, url)
    assert many == few, f"asset_reactivate: {few} → {many} consultas"
    with django_assert_max_num_queries(20):
        assert client.get(url).status_code == 200


def test_asset_decommission_queries_do_not_grow_with_components(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("asset_decommission", args=[tenant_a.asset.pk])
    _add_component_tree(tenant_a, 2)
    few = _count(client, url)
    _add_component_tree(tenant_a, 12)
    many = _count(client, url)
    assert many == few, f"asset_decommission (componentes): {few} → {many} consultas"


def test_asset_edit_queries_do_not_grow_with_rows(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("asset_edit", args=[tenant_a.asset.pk])
    _add_rows(tenant_a, 2)
    few = _count(client, url)
    _add_rows(tenant_a, 10)
    many = _count(client, url)
    assert many == few


def test_work_order_detail_query_budget(client, tenant_a, django_assert_max_num_queries):
    with tenant_context(tenant_a.org):
        wo = create_work_order(
            work_type="preventive", title="x", asset=tenant_a.asset, checklist=tenant_a.plan.items.all()
        )
    login(client, tenant_a.tech)
    with django_assert_max_num_queries(25):
        assert client.get(reverse("work_order_detail", args=[wo.pk])).status_code == 200


def _checklist_rows(tenant, wo, n):
    """Ítems con foto en la OT y planes publicados de la clase (WO-03)."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.maintenance.models import ItemType
    from apps.maintenance.services import create_plan
    from apps.work.services import add_checklist_item, add_checklist_photo
    from tests.conftest import tiny_jpeg_bytes

    with tenant_context(tenant.org):
        start = wo.checklist.count()
        for i in range(start, start + n):
            item = add_checklist_item(work_order=wo, membership=tenant.admin, text=f"Ítem {i}")
            photo = SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
            add_checklist_photo(item=item, membership=tenant.tech, uploaded_file=photo)
            create_plan(
                code=f"PX{i}",
                name=f"Plan {i}",
                asset_class=tenant.asset.asset_class,
                interval_days=30,
                items=[{"text": "x", "item_type": ItemType.CHECK}],
            )


@pytest.mark.parametrize("url_name", ["work_order_detail", "work_order_checklist"])
def test_checklist_screens_do_not_grow_with_items_photos_or_plans(
    client, tenant_a, settings, tmp_path, url_name
):
    settings.MEDIA_ROOT = tmp_path
    with tenant_context(tenant_a.org):
        wo = report_failure(
            membership=tenant_a.viewer, asset=tenant_a.asset, description="x", asset_stopped=False
        ).work_order
    login(client, tenant_a.admin)
    url = reverse(url_name, args=[wo.pk])
    _checklist_rows(tenant_a, wo, 2)
    few = _count(client, url)
    _checklist_rows(tenant_a, wo, 8)
    many = _count(client, url)
    assert many == few, f"{url_name}: {few} → {many} consultas"


def test_asset_list_in_labels_mode_does_not_grow_with_rows(client, tenant_a):
    """Modo etiquetas de la lista (AST-05 CA2): las casillas no agregan consultas por fila."""
    login(client, tenant_a.admin)
    url = f"{reverse('asset_list')}?labels=1"
    _add_rows(tenant_a, 2)
    few = _count(client, url)
    _add_rows(tenant_a, 10)
    assert _count(client, url) == few


def test_checklist_reorder_response_does_not_grow_with_items(client, tenant_a, settings, tmp_path):
    """Subir o bajar un ítem devuelve la lista completa (htmx) sin consultas por ítem (WO-03)."""
    settings.MEDIA_ROOT = tmp_path
    with tenant_context(tenant_a.org):
        wo = report_failure(
            membership=tenant_a.viewer, asset=tenant_a.asset, description="x", asset_stopped=False
        ).work_order
    login(client, tenant_a.admin)

    def move_last_up():
        with tenant_context(tenant_a.org):
            last = wo.checklist.order_by("-order").first()
        url = reverse("checklist_item_move", args=[wo.pk, last.pk])
        with CaptureQueriesContext(connection) as ctx:
            response = client.post(url, {"direction": "up"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        return len(ctx.captured_queries)

    _checklist_rows(tenant_a, wo, 2)
    few = move_last_up()
    _checklist_rows(tenant_a, wo, 8)
    many = move_last_up()
    # bulk_update escribe los ítems que cambian de lugar en una sola consulta.
    assert many == few, f"{few} → {many} consultas"
