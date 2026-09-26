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
            asset = Asset.objects.create(
                code=f"A-{Asset.objects.count():03d}",
                name=f"Equipo {i}",
                asset_class=tenant.asset.asset_class,
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
            location = Location.objects.create(code=f"L-{Asset.objects.count():03d}", name=f"Sitio {i}")
            MembershipLocation.objects.create(membership=member, location=location)


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
        "asset_labels",
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
