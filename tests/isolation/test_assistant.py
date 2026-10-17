"""Aislamiento del asistente de diagnóstico entre organizaciones y por sitio (AI-02 CA3, ADR-018, ADR-026).

Las herramientas solo devuelven lo que quien pregunta puede ver: el aislamiento lo garantiza
el backend, no el modelo.
"""

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset
from apps.assistant import selectors, services, tools
from apps.assistant.models import AssistantConversation, AssistantMessage
from apps.core.tenancy import CrossTenantError, tenant_context
from apps.organizations.models import Location
from apps.work.models import FailureEvent
from tests.assistant_utils import install
from tests.condition_utils import read, variable
from tests.conftest import login, make_manual, tiny_jpeg_bytes
from tests.inventory_utils import make_work_order


@pytest.fixture(autouse=True)
def configured(settings, monkeypatch, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.AI_ASSISTANT_USE_QUEUE = False
    install(monkeypatch)


def _seed(tenant, *, words="rodamiento ruidoso"):
    """Una OT con falla, un manual y una lectura sobre el activo, y una conversación del técnico."""
    with tenant_context(tenant.org):
        order = make_work_order(tenant, title=f"Cambio {words}", description=words)
        FailureEvent.objects.create(
            asset=tenant.asset,
            work_order=order,
            impact="partial",
            description=words,
            started_at=order.created_at,
        )
        manual = make_manual(tenant.asset, [f"Manual {words}"], by=tenant.admin.user)
        read(variable(tenant.asset), "70")
        photo = SimpleUploadedFile("placa.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
        conversation = services.start_conversation(
            membership=tenant.tech, asset=tenant.asset, text="Ruido", photo=photo
        )
    return order, manual, conversation


def _run(name, tool_input, *, membership, conversation):
    result = tools.run_tool(
        name=name, tool_input=tool_input, membership=membership, conversation=conversation
    )
    return result, json.loads(result.content)


def test_selectors_only_show_own_conversations(tenant_a, tenant_b):
    _seed(tenant_a)
    _order_b, _manual_b, conversation_b = _seed(tenant_b)
    with tenant_context(tenant_a.org):
        conversations = list(selectors.conversation_list(membership=tenant_a.admin))
        assert len(conversations) == 1 and conversations[0].organization_id == tenant_a.org.pk
        assert selectors.conversation_for(membership=tenant_a.admin, pk=conversation_b.pk) is None
        assert {m.organization_id for m in AssistantMessage.objects.all()} == {tenant_a.org.pk}
        message_b = AssistantMessage.all_tenants.filter(conversation=conversation_b).first()
        assert not selectors.message_is_visible(membership=tenant_a.admin, message_id=message_b.pk)
        assert selectors.quota_status(organization=tenant_a.org).used == 1


def test_services_reject_objects_of_another_organization(tenant_a, tenant_b):
    _order_b, _manual_b, conversation_b = _seed(tenant_b)
    with tenant_context(tenant_b.org):
        asset_b = Asset.objects.get()
        AssistantMessage.objects.filter(conversation=conversation_b).update(status="done")
    with tenant_context(tenant_a.org):
        a = tenant_a.tech
        attempts = [
            lambda: services.start_conversation(membership=a, asset=asset_b, text="x"),
            lambda: services.ask(membership=a, conversation=conversation_b, text="x"),
            lambda: services.propose_summary(membership=a, conversation=conversation_b),
            lambda: services.mark_helpful(membership=a, conversation=conversation_b, helpful="yes"),
        ]
        for attempt in attempts:
            with pytest.raises((PermissionDeniedError, CrossTenantError)):
                attempt()
        assert not AssistantConversation.objects.exists()
    with tenant_context(tenant_b.org):
        assert AssistantMessage.objects.filter(conversation=conversation_b).count() == 2


def test_tools_only_return_data_of_the_own_organization(tenant_a, tenant_b):
    order_a, manual_a, conversation_a = _seed(tenant_a)
    order_b, manual_b, _conversation_b = _seed(tenant_b)
    with tenant_context(tenant_b.org):
        Asset.objects.create(code="SOLO-B", name="Solo B", asset_class=tenant_b.asset.asset_class)
    with tenant_context(tenant_a.org):
        tech = tenant_a.tech
        result, data = _run(
            "search_work_orders",
            {"query": "rodamiento", "asset_only": False},
            membership=tech,
            conversation=conversation_a,
        )
        assert [s["work_order_id"] for s in result.sources] == [str(order_a.pk)]
        result, _ = _run(
            "search_manuals", {"query": "rodamiento"}, membership=tech, conversation=conversation_a
        )
        assert {s["attachment_id"] for s in result.sources} == {str(manual_a.pk)}
        result, _ = _run(
            "asset_failure_history", {"asset_code": ""}, membership=tech, conversation=conversation_a
        )
        assert [s["work_order_id"] for s in result.sources] == [str(order_a.pk)]
        for name in ("asset_failure_history", "recent_meter_readings"):
            result, data = _run(name, {"asset_code": "SOLO-B"}, membership=tech, conversation=conversation_a)
            assert result.is_error and not result.sources
        result, data = _run(
            "recent_meter_readings", {"asset_code": ""}, membership=tech, conversation=conversation_a
        )
        assert data["count"] == 1 and result.sources[0]["asset_id"] == str(tenant_a.asset.pk)


def test_tools_and_conversations_respect_the_site_scope(tenant_a):
    order_north, manual_north, _conversation = _seed(tenant_a, words="rodamiento norte")
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        tenant_a.asset.location = north
        tenant_a.asset.save(update_fields=["location"])
        south_asset = Asset.objects.create(
            code="SUR-01", name="Bomba sur", asset_class=tenant_a.asset.asset_class, location=south
        )
        order_south = make_work_order(tenant_a, asset=south_asset, title="Cambio rodamiento sur")
        FailureEvent.objects.create(
            asset=south_asset,
            work_order=order_south,
            impact="none",
            description="x",
            started_at=order_south.created_at,
        )
        manual_south = make_manual(south_asset, ["Manual rodamiento sur"], by=tenant_a.admin.user)
        read(variable(south_asset), "90")
        admin_on_south = services.start_conversation(membership=tenant_a.admin, asset=south_asset, text="Sur")
        own_on_south = services.start_conversation(membership=tenant_a.tech, asset=south_asset, text="Sur")

        MembershipLocation.objects.create(membership=tenant_a.tech, location=north)
        tenant_a.tech.restricted_to_locations = True
        tenant_a.tech.save(update_fields=["restricted_to_locations"])
        tech = tenant_a.tech
        conversation = services.start_conversation(membership=tech, asset=tenant_a.asset, text="Norte")

        result, _ = _run(
            "search_work_orders",
            {"query": "rodamiento", "asset_only": False},
            membership=tech,
            conversation=conversation,
        )
        assert {s["work_order_id"] for s in result.sources} == {str(order_north.pk)}
        result, _ = _run(
            "search_manuals", {"query": "rodamiento"}, membership=tech, conversation=conversation
        )
        assert {s["attachment_id"] for s in result.sources} == {str(manual_north.pk)}
        assert str(manual_south.pk) not in result.content
        for name in ("asset_failure_history", "recent_meter_readings"):
            result, _ = _run(name, {"asset_code": "SUR-01"}, membership=tech, conversation=conversation)
            assert result.is_error
        with pytest.raises(PermissionDeniedError):
            services.start_conversation(membership=tech, asset=south_asset, text="x")
        # Su propia conversación sobre un activo que ya no está en su alcance tampoco se ve.
        visible = set(selectors.conversation_list(membership=tech))
        assert own_on_south not in visible and admin_on_south not in visible and conversation in visible
        assert set(selectors.conversation_list(membership=tenant_a.admin)) >= {admin_on_south, own_on_south}


def test_each_user_sees_only_own_conversations_unless_admin(tenant_a):
    from tests.inventory_utils import add_member

    _seed(tenant_a)
    with tenant_context(tenant_a.org):
        other = add_member(tenant_a, "technician", suffix="2")
        theirs = services.start_conversation(membership=other, asset=tenant_a.asset, text="Otra")
        assert theirs not in selectors.conversation_list(membership=tenant_a.tech)
        assert selectors.conversation_for(membership=tenant_a.tech, pk=theirs.pk) is None
        assert theirs in selectors.conversation_list(membership=tenant_a.admin)
        assert list(selectors.conversation_list(membership=tenant_a.admin, mine_only=True)) == []


def test_photo_is_served_only_inside_the_owner_scope(client, tenant_a, tenant_b):
    from apps.attachments.models import Attachment

    _seed(tenant_a)
    _seed(tenant_b)
    with tenant_context(tenant_a.org):
        photo_a = Attachment.objects.get(target_type="assistant_message")
    with tenant_context(tenant_b.org):
        photo_b = Attachment.objects.get(target_type="assistant_message")
    login(client, tenant_a.tech)
    assert client.get(reverse("attachment", args=[photo_a.pk])).status_code == 200
    assert client.get(reverse("attachment", args=[photo_b.pk])).status_code == 404
    login(client, tenant_a.viewer)  # sin permiso del asistente
    assert client.get(reverse("attachment", args=[photo_a.pk])).status_code == 404


@pytest.mark.skipif(connection.vendor != "postgresql", reason="RLS solo existe en PostgreSQL")
def test_rls_hides_assistant_rows_of_other_tenant(tenant_a, tenant_b):
    _seed(tenant_a)
    _order_b, _manual_b, conversation_b = _seed(tenant_b)
    for model in (AssistantConversation, AssistantMessage):
        table = connection.ops.quote_name(model._meta.db_table)
        with tenant_context(tenant_a.org), connection.cursor() as cursor:
            cursor.execute(f"SELECT DISTINCT organization_id FROM {table}")
            assert {row[0] for row in cursor.fetchall()} == {tenant_a.org.pk}, model
    with tenant_context(tenant_a.org):
        with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
            AssistantMessage.objects.bulk_create(
                [
                    AssistantMessage(
                        organization=tenant_b.org, conversation_id=conversation_b.pk, role="user", seq=99
                    )
                ]
            )
