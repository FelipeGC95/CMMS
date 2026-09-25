"""Aislamiento entre organizaciones en vistas y archivos (NF-07)."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.work.services import report_failure
from tests.conftest import login


def _failure_in_a(tenant_a):
    photo = SimpleUploadedFile("f.jpg", b"\xff\xd8\xff fake", content_type="image/jpeg")
    with tenant_context(tenant_a.org):
        return report_failure(
            membership=tenant_a.viewer,
            asset=tenant_a.asset,
            description="Falla",
            asset_stopped=True,
            photo=photo,
        )


def test_user_of_b_gets_404_on_objects_of_a(client, tenant_a, tenant_b, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    request = _failure_in_a(tenant_a)
    with tenant_context(tenant_a.org):
        attachment = request.work_order.attachments.get()
        item_url = reverse("work_order_detail", args=[request.work_order_id])
    login(client, tenant_b.admin)
    for url in [
        reverse("asset_detail", args=[tenant_a.asset.pk]),
        reverse("asset_report", args=[tenant_a.asset.pk]),
        item_url,
        reverse("attachment", args=[attachment.pk]),
        reverse("asset_qr", args=[tenant_a.asset.qr_token]),
    ]:
        assert client.get(url).status_code == 404, url


def test_lists_only_show_own_organization(client, tenant_a, tenant_b):
    _failure_in_a(tenant_a)
    login(client, tenant_b.admin)
    assets = client.get(reverse("asset_list")).context["page"].object_list
    assert [a.pk for a in assets] == [tenant_b.asset.pk]
    work_orders = client.get(reverse("work_order_list")).context["page"].object_list
    assert list(work_orders) == []


def test_transition_on_foreign_work_order_is_404(client, tenant_a, tenant_b):
    request = _failure_in_a(tenant_a)
    login(client, tenant_b.admin)
    url = reverse("work_order_transition", args=[request.work_order_id])
    assert client.post(url, {"to": "cancelled"}).status_code == 404


def test_switching_to_foreign_organization_is_ignored(client, tenant_a, tenant_b):
    login(client, tenant_b.admin)
    client.post(reverse("switch_organization"), {"organization": str(tenant_a.org.id)})
    response = client.get(reverse("dashboard"))
    assert response.context["organization"] == tenant_b.org


def test_last_organization_without_membership_is_ignored(client, tenant_a, tenant_b):
    user = tenant_b.admin.user
    user.last_organization = tenant_a.org
    user.save()
    login(client, tenant_b.admin)
    assert client.get(reverse("dashboard")).context["organization"] == tenant_b.org


def test_qr_of_other_own_organization_switches_context(client, tenant_a, tenant_b):
    from apps.accounts.models import Membership

    Membership.objects.create(user=tenant_b.admin.user, organization=tenant_a.org, role="viewer")
    login(client, tenant_b.admin)
    response = client.get(reverse("asset_qr", args=[tenant_a.asset.qr_token]), follow=True)
    assert response.context["asset"] == tenant_a.asset


def test_team_views_do_not_cross_organizations(client, tenant_a, tenant_b):
    from apps.accounts.services import invite_member

    invitation = invite_member(
        membership=tenant_a.admin, email="solo-a@correo.cl", role="viewer", base_url="http://testserver/"
    )
    login(client, tenant_b.admin)
    page = client.get(reverse("team")).content.decode()
    assert tenant_a.tech.user.email not in page and "solo-a@correo.cl" not in page
    assert client.get(reverse("member_edit", args=[tenant_a.tech.pk])).status_code == 404
    assert client.post(reverse("member_active", args=[tenant_a.tech.pk]), {"active": "0"}).status_code == 404
    assert client.post(reverse("invitation_revoke", args=[invitation.pk])).status_code == 404
    tenant_a.tech.refresh_from_db()
    invitation.refresh_from_db()
    assert tenant_a.tech.is_active and invitation.revoked_at is None


def test_scope_cannot_reference_a_location_of_another_organization(tenant_a, tenant_b):
    import pytest

    from apps.accounts.services import TeamError, update_member
    from apps.organizations.models import Location

    with tenant_context(tenant_a.org):
        foreign = Location.objects.create(code="A-SITE", name="Sitio de A")
    with tenant_context(tenant_b.org), pytest.raises(TeamError):
        update_member(
            membership=tenant_b.admin,
            target=tenant_b.tech,
            role="technician",
            restricted_to_locations=True,
            locations=[foreign],
        )


def test_work_order_filters_do_not_reveal_another_organization(client, tenant_a, tenant_b):
    """Un id de sitio o de responsable de otra organización no filtra ni revela sus OT (WO-05)."""
    from apps.organizations.models import Location
    from apps.work.services import create_work_order

    with tenant_context(tenant_b.org):
        foreign_location = Location.objects.create(code="F", name="Sitio de B")
        foreign_wo = create_work_order(work_type="corrective", title="OT de B", asset=tenant_b.asset)

    login(client, tenant_a.admin)
    for params in [
        {"scope": "open", "location": str(foreign_location.pk)},
        {"scope": "open", "assigned_to": str(tenant_b.admin.user_id)},
        {"scope": "open", "asset_q": tenant_b.asset.code},
    ]:
        response = client.get(reverse("work_order_list"), params)
        assert response.status_code == 200
        assert foreign_wo.pk not in {wo.pk for wo in response.context["page"].object_list}
