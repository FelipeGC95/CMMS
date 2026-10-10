"""Las listas no hacen consultas N+1: el número de consultas no crece con los datos."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
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
        "asset_export_xlsx",
        "work_order_export_xlsx",
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


def test_filtered_dashboard_does_not_grow_with_rows(client, tenant_a):
    """Tablero con filtros (KPI-01): el cálculo no hace consultas por activo, falla u OT."""
    login(client, tenant_a.admin)
    params = {"period": "90", "criticality": "B", "asset_class": tenant_a.asset.asset_class_id}
    url = f"{reverse('dashboard')}?{urlencode(params)}"
    _add_rows(tenant_a, 2)
    few = _count(client, url)
    _add_rows(tenant_a, 10)
    many = _count(client, url)
    assert many == few


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
                calendar_interval=7,
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
                calendar_interval=7,
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
                calendar_interval=30,
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


def _add_plan_rows(tenant, usage_plan, n):
    """Planes con recursos y activos asignados (con medidores) o libres, para lista, ficha y selector."""
    from apps.assets.models import Meter
    from apps.maintenance.services import assign_plan, create_plan

    with tenant_context(tenant.org):
        for _i in range(n):
            k = Asset.objects.count()
            create_plan(
                code=f"P-{k}",
                name=f"Plan {k}",
                asset_class=tenant.asset.asset_class,
                calendar_interval=30,
                items=[{"text": "x"}],
                resources=[{"description": "Filtro"}],
            )
            location = Location.objects.create(code=f"L-{k}", name=f"Sitio {k}")
            for prefix in ("Q", "R"):  # Q: con el plan; R: libre para el selector
                asset = Asset.objects.create(
                    code=f"{prefix}-{k:03d}",
                    name=f"Equipo {k}",
                    asset_class=tenant.asset.asset_class,
                    location=location,
                )
                meter = Meter.objects.create(asset=asset, kind="hours", name="Motor", current_value=10)
                Meter.objects.create(asset=asset, kind="hours", name="Bomba", current_value=5)
                if prefix == "Q":
                    assign_plan(plan=tenant.plan, asset=asset)
                    assign_plan(plan=usage_plan, asset=asset, meter=meter)


@pytest.mark.parametrize("which", ["plan_list", "plan_detail", "plan_assign", "plan_assign_usage"])
def test_plan_screens_do_not_grow_with_rows(client, tenant_a, which):
    from apps.maintenance.services import create_draft, create_plan

    with tenant_context(tenant_a.org):
        usage = create_plan(
            code="USO", name="250 h", meter_kind="hours", meter_interval=250, items=[{"text": "x"}]
        )
        create_draft(membership=tenant_a.admin, plan=tenant_a.plan)
    login(client, tenant_a.admin)
    url = {
        "plan_list": reverse("plan_list"),
        "plan_detail": reverse("plan_detail", args=[tenant_a.plan.pk]),
        "plan_assign": reverse("plan_assign", args=[tenant_a.plan.pk]),
        "plan_assign_usage": f"{reverse('plan_assign', args=[usage.pk])}?q=",
    }[which]
    _add_plan_rows(tenant_a, usage, 2)
    few = _count(client, url)
    _add_plan_rows(tenant_a, usage, 10)
    many = _count(client, url)
    assert many == few, f"{which}: {few} → {many} consultas"


def _add_meter_rows(tenant, n):
    from apps.assets.models import Meter, MeterKind, MeterReading

    _add_rows(tenant, n)
    with tenant_context(tenant.org):
        for asset in Asset.objects.filter(meters__isnull=True):
            meter = Meter.objects.create(asset=asset, kind=MeterKind.HOURS)
            MeterReading.objects.create(meter=meter, value=1, read_at=asset.created_at)


def _api_count(client, url):
    with CaptureQueriesContext(connection) as ctx:
        assert client.get(url).status_code == 200
    # "Último uso" se escribe como mucho una vez por minuto: se cuentan solo las lecturas.
    return len([q for q in ctx.captured_queries if "last_used_at" not in q["sql"]])


@pytest.mark.parametrize("resource", ["assets", "meters", "readings", "work-orders", "requests"])
def test_api_lists_do_not_grow_with_rows(tenant_a, resource):
    """Listas de la API v1 (X-05): consultas acotadas sin importar cuántas filas haya."""
    from tests.api_utils import api_client

    client = api_client(tenant_a)
    url = f"/api/v1/{resource}/"
    _add_meter_rows(tenant_a, 2)
    few = _api_count(client, url)
    _add_meter_rows(tenant_a, 10)
    many = _api_count(client, url)
    assert many == few, f"{resource}: {few} → {many} consultas"


def test_reading_import_preview_does_not_grow_with_rows(client, tenant_a):
    """Vista previa del CSV de lecturas (AST-09): medidores y duplicados en consultas fijas."""
    import io

    _add_meter_rows(tenant_a, 12)
    with tenant_context(tenant_a.org):
        codes = list(Asset.objects.values_list("code", flat=True))
    login(client, tenant_a.admin)

    def preview(n):
        body = "asset_code,value,read_at\n" + "".join(f"{c},5,2026-09-20\n" for c in codes[:n])
        file = io.BytesIO(body.encode())
        file.name = "l.csv"
        with CaptureQueriesContext(connection) as ctx:
            assert client.post(reverse("reading_import"), {"file": file}).status_code == 200
        return len(ctx.captured_queries)

    assert preview(2) == preview(12)


def _typed_items(tenant, wo, n):
    """Ítems de texto, foto obligatoria y firma ya respondidos (PM-01 CA1)."""
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    from apps.maintenance.models import ItemType
    from apps.work.services import add_checklist_item, add_checklist_photo, answer_checklist_item
    from tests.conftest import tiny_jpeg_bytes

    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), (255, 255, 255)).save(buffer, format="PNG")
    with tenant_context(tenant.org):
        start = wo.checklist.count()
        for i in range(start, start + n):
            text = add_checklist_item(
                work_order=wo, membership=tenant.admin, text=f"T{i}", item_type=ItemType.TEXT
            )
            answer_checklist_item(item=text, membership=tenant.tech, value_text="ok")
            photo = add_checklist_item(
                work_order=wo, membership=tenant.admin, text=f"F{i}", item_type=ItemType.PHOTO
            )
            jpeg = SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
            add_checklist_photo(item=photo, membership=tenant.tech, uploaded_file=jpeg)
            sign = add_checklist_item(
                work_order=wo, membership=tenant.admin, text=f"S{i}", item_type=ItemType.SIGNATURE
            )
            png = SimpleUploadedFile("s.png", buffer.getvalue(), content_type="image/png")
            answer_checklist_item(item=sign, membership=tenant.tech, signature=png)


def test_checklist_with_new_item_types_does_not_grow(client, tenant_a, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
    login(client, tenant_a.tech)
    url = reverse("work_order_detail", args=[wo.pk])
    _typed_items(tenant_a, wo, 1)
    few = _count(client, url)
    _typed_items(tenant_a, wo, 5)
    many = _count(client, url)
    assert many == few, f"checklist con tipos nuevos: {few} → {many} consultas"


@pytest.mark.parametrize("suffix", ["checklist-items", "attachments"])
def test_api_work_order_checklist_and_attachments_do_not_grow(tenant_a, settings, tmp_path, suffix):
    """Listas de la API v1 de una OT (ítems con su conteo de fotos y adjuntos): consultas acotadas."""
    from tests.api_utils import api_client

    settings.MEDIA_ROOT = tmp_path
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
    client = api_client(tenant_a)
    url = f"/api/v1/work-orders/{wo.pk}/{suffix}/"
    _typed_items(tenant_a, wo, 1)
    few = _api_count(client, url)
    _typed_items(tenant_a, wo, 5)
    many = _api_count(client, url)
    assert many == few, f"{suffix}: {few} → {many} consultas"


def _absorbing_assets(tenant, n, plans):
    """n activos nuevos con un plan mayor que absorbe a uno menor, ambos por vencer (PM-05 CA3)."""
    import datetime as dt

    from apps.maintenance.services import assign_plan

    with tenant_context(tenant.org):
        for _ in range(n):
            asset = Asset.objects.create(
                code=f"GA-{Asset.objects.count():03d}", name="Grúa", asset_class=tenant.asset.asset_class
            )
            assign_plan(plan=plans[0], asset=asset, starts_on=dt.date(2026, 10, 1))
            assign_plan(plan=plans[1], asset=asset, starts_on=dt.date(2026, 10, 3))


def _generation_queries(tenant):
    import datetime as dt

    from apps.maintenance.services import generate_due_work_orders

    with tenant_context(tenant.org), CaptureQueriesContext(connection) as ctx:
        created = generate_due_work_orders(today=dt.date(2026, 9, 24), horizon_days=14)
    return len(ctx.captured_queries), len(created)


def test_generation_with_absorption_has_a_constant_cost_per_work_order(tenant_a):
    from apps.maintenance.models import CalendarUnit
    from apps.maintenance.services import add_plan_absorption, create_plan

    with tenant_context(tenant_a.org):
        items = [{"text": "Revisar", "item_type": "check"}, {"text": "Nota", "item_type": "text"}]
        major = create_plan(
            code="G6", name="6 meses", calendar_interval=6, calendar_unit=CalendarUnit.MONTHS, items=items
        )
        minor = create_plan(
            code="G1", name="1 mes", calendar_interval=1, calendar_unit=CalendarUnit.MONTHS, items=items
        )
        add_plan_absorption(membership=tenant_a.admin, plan=major, absorbed_plan=minor)
    _absorbing_assets(tenant_a, 1, (major, minor))
    _generation_queries(tenant_a)  # la primera OT crea el correlativo: se excluye de la medición
    counts = []
    for n in (1, 2, 4):
        _absorbing_assets(tenant_a, n, (major, minor))
        queries, created = _generation_queries(tenant_a)
        assert created == n
        counts.append(queries)
    per_order = counts[1] - counts[0]
    assert counts[2] - counts[1] == 2 * per_order, f"generación: {counts}"  # lineal: sin N+1 por candidato
    assert per_order <= 15, f"{per_order} consultas por OT generada"
    # Sin nada que generar (todas con OT abierta), el costo no depende de cuántas asignaciones hay.
    idle = _generation_queries(tenant_a)[0]
    _absorbing_assets(tenant_a, 3, (major, minor))
    _generation_queries(tenant_a)
    assert _generation_queries(tenant_a)[0] == idle


def _add_calendar_rows(tenant, n):
    """Activos con un plan mayor que absorbe a uno menor, un plan por medidor y una OT abierta."""
    import datetime as dt
    from decimal import Decimal

    from django.utils import timezone

    from apps.assets.models import Meter
    from apps.maintenance.models import MaintenancePlan
    from apps.maintenance.services import add_plan_absorption, assign_plan, create_plan

    item = [{"text": "Revisar", "item_type": "check"}]
    today = timezone.localdate()
    with tenant_context(tenant.org):
        for i in range(n):
            k = MaintenancePlan.objects.count()
            big = create_plan(
                code=f"CB-{k}", name=f"Mayor {i}", calendar_interval=3, calendar_unit="months", items=item
            )
            small = create_plan(
                code=f"CS-{k}", name=f"Menor {i}", calendar_interval=2, calendar_unit="weeks", items=item
            )
            hours = create_plan(
                code=f"CH-{k}", name=f"Horas {i}", meter_kind="hours", meter_interval=250, items=item
            )
            add_plan_absorption(membership=tenant.admin, plan=big, absorbed_plan=small)
            asset = Asset.objects.create(
                code=f"CAL-{Asset.objects.count():03d}",
                name=f"Equipo {i}",
                asset_class=tenant.asset.asset_class,
            )
            meter = Meter.objects.create(
                asset=asset, kind="hours", current_value=Decimal(100), avg_daily_usage=Decimal(9)
            )
            assign_plan(plan=big, asset=asset, starts_on=today + dt.timedelta(days=i))
            assign_plan(plan=small, asset=asset, starts_on=today + dt.timedelta(days=i))
            assign_plan(plan=hours, asset=asset, meter=meter)
            create_work_order(
                work_type="preventive", title=f"Prev {i}", asset=asset, status="planned", due_on=today
            )


@pytest.mark.parametrize(
    "url_name,params",
    [
        ("plan_calendar", {}),
        ("plan_calendar", {"view": "year"}),
        ("plan_calendar", {"view": "year", "asset": "CAL"}),
        ("plan_calendar_export_xlsx", {}),
    ],
)
def test_plan_calendar_queries_do_not_grow_with_assignments(client, tenant_a, url_name, params):
    """Calendario de preventivos (PM-06): la proyección se calcula en memoria, sin consultas por fila."""
    login(client, tenant_a.admin)
    url = f"{reverse(url_name)}?{urlencode(params)}"
    _add_calendar_rows(tenant_a, 2)
    few = _count(client, url)
    _add_calendar_rows(tenant_a, 10)
    many = _count(client, url)
    assert many == few, f"{url_name} {params}: {few} → {many} consultas"


def _add_plans(tenant, start, n):
    from apps.maintenance.services import create_plan

    with tenant_context(tenant.org):
        for i in range(start, start + n):
            create_plan(
                code=f"XP-{i:03d}",
                name=f"Plan {i}",
                calendar_interval=1,
                calendar_unit="months",
                items=[{"text": "A", "item_type": "check"}, {"text": "B", "item_type": "check"}],
                resources=[{"kind": "part", "description": "Filtro", "quantity": 1}],
            )


def test_plan_export_queries_do_not_grow_with_plans(client, tenant_a):
    """Exportar planes a Excel (PM-08): ítems y recursos por lotes, no por plan."""
    login(client, tenant_a.admin)
    url = reverse("plan_export")
    _add_plans(tenant_a, 0, 2)
    few = _count(client, url)
    _add_plans(tenant_a, 2, 10)
    many = _count(client, url)
    assert many == few, f"{few} → {many} consultas"


def test_plan_import_preview_queries_do_not_grow_with_plans(client, tenant_a):
    """Vista previa de importación de planes (PM-08): compara con lo existente por lotes."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    login(client, tenant_a.admin)

    def upload_count():
        content = client.get(reverse("plan_export")).content
        with CaptureQueriesContext(connection) as ctx:
            response = client.post(reverse("plan_import"), {"file": SimpleUploadedFile("p.xlsx", content)})
        assert response.status_code == 200
        return len(ctx.captured_queries)

    _add_plans(tenant_a, 0, 2)
    few = upload_count()
    _add_plans(tenant_a, 2, 10)
    many = upload_count()
    assert many == few, f"{few} → {many} consultas"


@pytest.mark.parametrize("industry", ["fleet", "industrial", "food", "buildings"])
def test_industry_template_apply_uses_bounded_queries(tenant_a, industry, django_assert_max_num_queries):
    """Aplicar una plantilla de rubro (ORG-03 CA3): por lotes, sin consultas por clase, plan o ítem."""
    from apps.maintenance.industry_templates import apply_industry_template

    with django_assert_max_num_queries(30):
        summary = apply_industry_template(organization=tenant_a.org, industry=industry)
    assert summary.plans


def test_settings_and_delete_pages_do_not_grow_with_exports(client, tenant_a):
    """Tarjeta de exportaciones (X-03): las últimas 5, con quien las pidió, sin N+1."""
    from apps.organizations.models import DataExport

    def add(n):
        with tenant_context(tenant_a.org):
            for _ in range(n):
                DataExport.objects.create(requested_by=tenant_a.admin.user, status=DataExport.Status.EXPIRED)

    login(client, tenant_a.admin)
    add(1)
    few = {name: _count(client, reverse(name)) for name in ("organization_settings", "organization_delete")}
    add(8)
    assert {name: _count(client, reverse(name)) for name in few} == few


def test_notification_list_and_bell_are_bounded(client, tenant_a, django_assert_max_num_queries):
    """Bandeja (X-02) sin N+1 y campana con una sola consulta de conteo."""
    from apps.notifications.models import Kind, Notification
    from apps.notifications.selectors import unread_count

    def add(n):
        with tenant_context(tenant_a.org):
            for i in range(n):
                Notification.objects.create(
                    recipient=tenant_a.admin.user, kind=Kind.WO_ASSIGNED, title=f"A {i}"
                )

    login(client, tenant_a.admin)
    add(2)
    few = _count(client, reverse("notification_list"))
    add(15)
    assert _count(client, reverse("notification_list")) == few
    with tenant_context(tenant_a.org), django_assert_max_num_queries(1):
        assert unread_count(membership=tenant_a.admin) == 17
    badge_queries = _count(client, reverse("notification_badge"))
    add(5)
    assert _count(client, reverse("notification_badge")) == badge_queries


def _onboarding_rows(tenant, n):
    """Activos con medidor y plan por uso (paso de lecturas) y ubicaciones (paso de sitios)."""
    from apps.assets.models import AssetClass
    from apps.onboarding import services as onboarding

    with tenant_context(tenant.org):
        onboarding.choose_industry(membership=tenant.admin, industry="fleet")
        forklift = AssetClass.objects.get(code="GRH-COMB")
        start = Asset.objects.count()
        onboarding.quick_add_assets(
            membership=tenant.admin,
            rows=[
                {
                    "name": f"Grúa {start + i}",
                    "asset_class": forklift,
                    "location": None,
                    "meter_kind": "hours",
                }
                for i in range(n)
            ],
        )
        onboarding.create_locations(
            membership=tenant.admin, site_name=f"Sitio {start}", areas=[f"Área {i}" for i in range(n)]
        )
        onboarding.create_suggested_plans(membership=tenant.admin, plan_codes=["GRH-C-250H"])


@pytest.mark.parametrize("step", ["industry", "locations", "assets", "plans", "readings", "calendar"])
def test_onboarding_steps_do_not_grow_with_rows(client, tenant_a, step):
    login(client, tenant_a.admin)
    url = reverse("onboarding_step", args=[step])
    client.get(url)  # la primera visita marca el asistente como abierto (una escritura)
    _onboarding_rows(tenant_a, 2)
    few = _count(client, url)
    _onboarding_rows(tenant_a, 8)
    many = _count(client, url)
    assert many == few, f"{step}: {few} → {many} consultas"


def _add_manuals(tenant, n):
    """Manuales del activo y de su clase, cada uno con una página que contiene "bomba" (AST-06 CA3)."""
    from tests.conftest import make_manual

    with tenant_context(tenant.org):
        for i in range(n):
            make_manual(tenant.asset, [f"bomba {i}"], by=tenant.admin.user, name=f"a{i}.pdf")
            make_manual(
                tenant.asset.asset_class, [f"bomba clase {i}"], by=tenant.admin.user, name=f"c{i}.pdf"
            )


@pytest.mark.parametrize("which", ["asset_detail", "asset_class_detail", "manual_search"])
def test_manual_lists_and_search_do_not_grow_with_rows(client, tenant_a, settings, tmp_path, which):
    settings.MEDIA_ROOT = tmp_path
    login(client, tenant_a.admin)
    if which == "asset_detail":
        url = reverse(which, args=[tenant_a.asset.pk])
    elif which == "asset_class_detail":
        url = reverse(which, args=[tenant_a.asset.asset_class_id])
    else:
        url = reverse(which) + "?q=bomba"
    _add_manuals(tenant_a, 2)
    few = _count(client, url)
    _add_manuals(tenant_a, 8)
    many = _count(client, url)
    assert many == few, f"{which}: {few} → {many} consultas"


def _add_planning_rows(tenant, n, monday):
    """OT programadas y sin programar, con técnicos con alcance por sitio (REQ-04/05)."""
    from apps.work.services import schedule_work_order

    with tenant_context(tenant.org):
        for i in range(n):
            site = Location.objects.create(code=f"PL-{Location.objects.count():03d}", name=f"Planta {i}")
            asset = Asset.objects.create(
                code=f"PA-{Asset.objects.count():03d}",
                name=f"Equipo {i}",
                asset_class=tenant.asset.asset_class,
                location=site,
                criticality="ABC"[i % 3],
            )
            user = User.objects.create_user(f"pt{User.objects.count()}@{tenant.org.slug}.cl")
            tech = Membership.objects.create(
                user=user, organization=tenant.org, role="technician", restricted_to_locations=True
            )
            MembershipLocation.objects.create(membership=tech, location=site)
            for day in range(2):
                wo = create_work_order(
                    work_type="corrective",
                    title=f"Programada {i}",
                    asset=asset,
                    assigned_to=user,
                    helpers=[tenant.tech.user],
                    estimated_minutes=120,
                )
                schedule_work_order(
                    work_order=wo,
                    membership=tenant.admin,
                    scheduled_for=monday + datetime.timedelta(days=day),
                )
            create_work_order(
                work_type="inspection", title=f"Sin día {i}", location=site, estimated_minutes=60, priority=1
            )


@pytest.mark.parametrize(
    "url_name,who",
    [
        ("work_order_backlog", "admin"),
        ("work_order_backlog_export_xlsx", "admin"),
        ("work_order_board", "admin"),
        ("work_order_board_lanes", "admin"),
        ("work_order_my_week", "tech"),
    ],
)
def test_planning_screens_do_not_grow_with_rows(client, tenant_a, url_name, who):
    """Backlog, tablero semanal y «Mi semana» (REQ-04/05): consultas fijas."""
    from apps.work.selectors import week_start

    monday = week_start(timezone.localdate()) + datetime.timedelta(days=7)
    login(client, getattr(tenant_a, who))
    # Tablero «Por técnico»: una fila por técnico sin consultas por técnico.
    lanes = url_name == "work_order_board_lanes"
    url = f"{reverse('work_order_board' if lanes else url_name)}?week={monday.isoformat()}"
    if lanes:
        url += "&lanes=1"
    _add_planning_rows(tenant_a, 2, monday)
    few = _count(client, url)
    _add_planning_rows(tenant_a, 6, monday)
    many = _count(client, url)
    assert many == few, f"{url_name}: {few} → {many} consultas"


def _add_anonymous_requests(tenant, n, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.work.services import report_anonymous_failure
    from tests.conftest import tiny_jpeg_bytes

    with tenant_context(tenant.org):
        for i in range(n):
            photo = SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
            report_anonymous_failure(
                asset=tenant.asset, name=f"Persona {i}", description="x", asset_stopped=True, photo=photo
            )


def test_request_inbox_with_anonymous_photos_does_not_grow(client, tenant_a, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    tenant_a.org.allow_anonymous_reports = True
    tenant_a.org.save()
    login(client, tenant_a.admin)
    _add_anonymous_requests(tenant_a, 2, tmp_path)
    few = _count(client, reverse("request_list"))
    _add_anonymous_requests(tenant_a, 10, tmp_path)
    assert _count(client, reverse("request_list")) == few


def test_public_qr_page_query_budget(client, tenant_a, django_assert_max_num_queries):
    tenant_a.org.allow_anonymous_reports = True
    tenant_a.org.save()
    url = reverse("asset_qr", args=[tenant_a.asset.qr_token])
    with django_assert_max_num_queries(3):  # sesión + activo con su organización y plan
        assert client.get(url).status_code == 200


def _field_queries(tenant, membership):
    """Consultas del snapshot sin conexión (X-06) y de sus catálogos, recorriendo lo que usa el teléfono."""
    from apps.work.selectors import field_reference, field_work_orders

    with tenant_context(tenant.org), CaptureQueriesContext(connection) as ctx:
        snapshot = field_work_orders(membership=membership, days=14)
        for wo in snapshot.work_orders:
            str(wo.asset.location if wo.asset_id else wo.location)
            [str(user) for user in wo.helpers.all()]
            [(item.photo_count, str(item.answered_by)) for item in wo.field_checklist]
            (wo.field_meter, wo.meter_required, wo.assigned_to_me, str(wo.assigned_to))
        field_reference(membership=membership)  # devuelve listas ya evaluadas
    return len(ctx.captured_queries), len(snapshot.work_orders)


def test_field_snapshot_and_reference_do_not_grow_with_rows(tenant_a, settings, tmp_path):
    from apps.assets.models import Meter, MeterKind

    settings.MEDIA_ROOT = tmp_path

    def add(n):
        _add_rows(tenant_a, n)
        with tenant_context(tenant_a.org):
            for asset in Asset.objects.filter(meters__isnull=True):
                Meter.objects.create(asset=asset, kind=MeterKind.HOURS)
            wo = create_work_order(work_type="corrective", title="c", asset=tenant_a.asset)
        _typed_items(tenant_a, wo, n)

    add(1)
    few, rows_few = _field_queries(tenant_a, tenant_a.tech)
    add(4)
    many, rows_many = _field_queries(tenant_a, tenant_a.tech)
    assert rows_many > rows_few
    assert many == few, f"snapshot sin conexión: {few} → {many} consultas"
    assert few <= 12, f"snapshot sin conexión: {few} consultas"


def _field_api_queries(client, url):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200, url
    return len(ctx.captured_queries)


@pytest.mark.parametrize("path", ["snapshot/?days=14", "reference/", "meters/", "checklist-items"])
def test_field_api_queries_do_not_grow_with_rows(
    tenant_a, settings, tmp_path, path, django_assert_max_num_queries
):
    """API de terreno (X-06): snapshot, catálogos y listas con consultas acotadas."""
    from apps.assets.models import Meter, MeterKind
    from tests.api_utils import FIELD_API, field_client

    settings.MEDIA_ROOT = tmp_path
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="c", asset=tenant_a.asset)

    def add(n):
        _add_rows(tenant_a, n)
        with tenant_context(tenant_a.org):
            for asset in Asset.objects.filter(meters__isnull=True):
                Meter.objects.create(asset=asset, kind=MeterKind.HOURS)
        _typed_items(tenant_a, wo, n)

    url = (
        f"{FIELD_API}/work-orders/{wo.pk}/checklist-items/"
        if path == "checklist-items"
        else f"{FIELD_API}/{path}"
    )
    client = field_client(tenant_a.tech)
    add(1)
    few = _field_api_queries(client, url)
    add(4)
    many = _field_api_queries(client, url)
    assert many == few, f"{url}: {few} → {many} consultas"
    with django_assert_max_num_queries(16):
        client.get(url)


def _add_kpi_rows(tenant, n):
    """Por fila: activo de otra clase en un sitio con técnico limitado, OT pendiente, OT cerrada con
    costos y horas, OT cancelada con su cambio de estado y fallas con y sin código (KPI-02)."""
    from apps.assets.models import AssetClass
    from apps.work.models import FailureEvent, LaborEntry, WorkOrder, WorkOrderTransition

    yesterday = timezone.now() - datetime.timedelta(days=1)
    with tenant_context(tenant.org):
        for _i in range(n):
            k = Asset.objects.count()
            site = Location.objects.create(code=f"K-{k:03d}", name=f"Sitio {k}")
            cls = AssetClass.objects.create(code=f"K{k:03d}", name=f"Clase {k}")
            asset = Asset.objects.create(
                code=f"K-{k:03d}", name=f"Equipo {k}", asset_class=cls, location=site
            )
            create_work_order(work_type="corrective", title="p", asset=asset, estimated_minutes=60)
            done = create_work_order(work_type="preventive", title="c", asset=asset)
            LaborEntry.objects.create(work_order=done, user=tenant.tech.user, minutes=30)
            WorkOrder.objects.filter(pk=done.pk).update(
                status="closed", completed_at=yesterday, closed_at=yesterday, total_cost=k, parts_cost=k
            )
            cancelled = create_work_order(
                work_type="corrective", title="x", asset=asset, estimated_minutes=30
            )
            WorkOrder.objects.filter(pk=cancelled.pk).update(status="cancelled")
            WorkOrderTransition.objects.create(
                work_order=cancelled, from_status="planned", to_status="cancelled"
            )
            mode = create_failure_code(
                kind=FailureCode.Kind.MODE, code=f"K{k:03d}", name=f"Modo {k}", membership=tenant.admin
            )
            FailureEvent.objects.create(
                asset=asset,
                impact="total_stop",
                failure_mode=mode,
                started_at=yesterday,
                ended_at=timezone.now(),
            )
            FailureEvent.objects.create(asset=asset, impact="partial", started_at=yesterday)
            user = User.objects.create_user(f"k{k}@{tenant.org.slug}.cl")
            member = Membership.objects.create(
                user=user, organization=tenant.org, role="technician", restricted_to_locations=True
            )
            MembershipLocation.objects.create(membership=member, location=site)


@pytest.mark.parametrize(
    "url_name, params",
    [
        ("kpi_report", {}),
        ("kpi_report", {"costs": "all", "pareto": "cause", "pareto_by": "downtime", "period": "365"}),
        ("kpi_report", {"criticality": "B", "period": "90"}),
        ("kpi_report_export_xlsx", {"period": "365"}),
    ],
)
def test_extended_kpis_do_not_grow_with_rows(
    client, tenant_a, url_name, params, django_assert_max_num_queries
):
    """KPI-02 CA7: cada indicador en un número fijo de consultas agregadas (sin consultas por OT,
    activo, falla o técnico)."""
    login(client, tenant_a.admin)
    url = f"{reverse(url_name)}?{urlencode(params)}"
    _add_kpi_rows(tenant_a, 2)
    few = _count(client, url)
    _add_kpi_rows(tenant_a, 10)
    many = _count(client, url)
    assert many == few, f"{url}: {few} → {many} consultas"
    with django_assert_max_num_queries(30):
        client.get(url)


def test_inventory_stock_list_and_part_detail_are_bounded(tenant_a, django_assert_max_num_queries):
    """INV-01/04: la lista de stock y la ficha no crecen con repuestos, bodegas ni movimientos."""
    from apps.inventory import services as inventory
    from apps.inventory.selectors import part_detail, stock_list

    with tenant_context(tenant_a.org):
        main = inventory.ensure_default_warehouse()
        other = inventory.create_warehouse(membership=tenant_a.admin, code="B2", name="Bodega 2")
        parts = [
            inventory.create_part(membership=tenant_a.admin, code=f"P{i}", name=f"P{i}") for i in range(12)
        ]
        for part in parts:
            inventory.receive_stock(membership=tenant_a.admin, part=part, quantity=3, unit_cost="5")
            inventory.transfer_stock(
                membership=tenant_a.admin, part=part, from_warehouse=main, to_warehouse=other, quantity=1
            )
        with django_assert_max_num_queries(1):
            rows = list(stock_list(membership=tenant_a.admin, status="below_min"))
        with django_assert_max_num_queries(1):
            rows = list(stock_list(membership=tenant_a.admin)[:10])
        assert [row.on_hand_total for row in rows] == [3] * 10
        with django_assert_max_num_queries(3):
            detail = part_detail(parts[0].pk, membership=tenant_a.admin)
            [(m.warehouse.code, m.created_by) for m in detail.movements]
