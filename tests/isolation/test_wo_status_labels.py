"""Los nombres de estados de OT de una organización nunca se ven en otra (WO-02 CA5)."""

from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.work.services import report_failure
from tests.conftest import login


def _work_order(tenant):
    with tenant_context(tenant.org):
        return report_failure(
            membership=tenant.viewer, asset=tenant.asset, description="Falla", asset_stopped=False
        ).work_order


def test_labels_of_a_never_show_in_b(client, tenant_a, tenant_b):
    _work_order(tenant_a)
    wo_b = _work_order(tenant_b)
    login(client, tenant_a.admin)
    client.post(
        reverse("organization_settings"), {"section": "status_labels", "label-requested": "Etiqueta de A"}
    )
    tenant_a.org.refresh_from_db()
    tenant_b.org.refresh_from_db()
    assert tenant_a.org.work_order_status_labels == {"requested": "Etiqueta de A"}
    assert tenant_b.org.work_order_status_labels == {}

    assert "Etiqueta de A" in client.get(reverse("work_order_list")).content.decode()

    login(client, tenant_b.admin)
    for url in [
        reverse("work_order_list"),
        reverse("work_order_detail", args=[wo_b.pk]),
        reverse("organization_settings"),
    ]:
        assert "Etiqueta de A" not in client.get(url).content.decode(), url
