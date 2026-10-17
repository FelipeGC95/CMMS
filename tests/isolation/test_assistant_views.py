"""Aislamiento de las pantallas del asistente (AI-02 CA3): la organización A no abre, no pregunta
ni ve fotos de conversaciones de B, ni pide ayuda sobre activos u OT de B, ni siquiera por URL."""

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.assistant import services
from apps.assistant.models import AssistantConversation, AssistantMessage
from apps.attachments.models import Attachment, TargetType
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from tests.assistant_utils import install
from tests.conftest import login, tiny_jpeg_bytes
from tests.inventory_utils import make_work_order


@pytest.fixture(autouse=True)
def configured(settings, monkeypatch, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.AI_ASSISTANT_USE_QUEUE = False
    install(monkeypatch)


def _conversation_with_photo(tenant):
    with tenant_context(tenant.org):
        order = make_work_order(tenant)
        photo = SimpleUploadedFile("placa.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
        conversation = services.start_conversation(
            membership=tenant.tech, asset=tenant.asset, work_order=order, text="Ruido", photo=photo
        )
        AssistantMessage.objects.filter(conversation=conversation).update(status="done")
        photo_pk = Attachment.objects.get(target_type=TargetType.ASSISTANT_MESSAGE).pk
    return order, conversation, photo_pk


@pytest.mark.parametrize("role", ["admin", "tech"])
def test_organization_a_cannot_open_or_act_on_conversations_of_b(client, tenant_a, tenant_b, role):
    _order_b, conversation_b, photo_b = _conversation_with_photo(tenant_b)
    login(client, getattr(tenant_a, role))
    pk = conversation_b.pk
    for name in ("assistant_conversation", "assistant_thread"):
        assert client.get(reverse(name, args=[pk])).status_code == 404
    for name, data in (
        ("assistant_ask", {"text": "x", "client_uuid": uuid.uuid4()}),
        ("assistant_summary", {}),
        ("assistant_helpful", {"helpful": "yes"}),
        ("assistant_close", {}),
    ):
        assert client.post(reverse(name, args=[pk]), data).status_code == 404
    assert client.get(reverse("attachment", args=[photo_b])).status_code == 404
    assert (
        reverse("assistant_conversation", args=[pk])
        not in client.get(reverse("assistant_list") + "?scope=all").text
    )
    with tenant_context(tenant_b.org):
        conversation_b.refresh_from_db()
        assert (conversation_b.status, conversation_b.helpful) == ("open", "")
        assert AssistantMessage.objects.filter(conversation=conversation_b).count() == 2


def test_organization_a_cannot_ask_about_assets_or_work_orders_of_b(client, tenant_a, tenant_b):
    order_b, _conversation_b, _photo = _conversation_with_photo(tenant_b)
    login(client, tenant_a.tech)
    asset_b = f"{reverse('assistant_start')}?asset={tenant_b.asset.pk}"
    assert client.get(asset_b).status_code == 404
    assert client.post(asset_b, {"text": "x", "client_uuid": uuid.uuid4()}).status_code == 404
    wo_b = f"{reverse('assistant_start')}?asset={tenant_a.asset.pk}&work_order={order_b.pk}"
    assert client.get(wo_b).status_code == 404
    assert client.post(wo_b, {"text": "x", "client_uuid": uuid.uuid4()}).status_code == 404
    with tenant_context(tenant_a.org):
        assert not AssistantConversation.objects.exists()


def test_site_scope_hides_conversations_about_assets_out_of_scope(client, tenant_a):
    """Con alcance por sitio, ni el administrador ve una conversación de un activo fuera de él."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        tenant_a.asset.location = north
        tenant_a.asset.save(update_fields=["location"])
    _order, conversation, photo = _conversation_with_photo(tenant_a)
    with tenant_context(tenant_a.org):
        MembershipLocation.objects.create(membership=tenant_a.admin, location=south)
        tenant_a.admin.restricted_to_locations = True
        tenant_a.admin.save(update_fields=["restricted_to_locations"])
    login(client, tenant_a.admin)
    assert client.get(reverse("assistant_conversation", args=[conversation.pk])).status_code == 404
    assert client.get(reverse("attachment", args=[photo])).status_code == 404
    assert client.get(f"{reverse('assistant_start')}?asset={tenant_a.asset.pk}").status_code == 404
