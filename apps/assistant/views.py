"""Pantallas del asistente de diagnóstico (AI-01..05, ADR-018).

Vistas delgadas: validan el formulario y llaman a los servicios; las lecturas pasan por los
selectores (permisos y alcance por sitio). El módulo `assistant` lo exige el middleware de
módulos para toda ruta bajo /assistant/. La respuesta llega sin bloquear: mientras haya un
mensaje pendiente, la conversación se vuelve a pedir cada ~2 s con htmx (`_thread.html`).
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm, PermissionDeniedError
from apps.assets.selectors import visible_assets
from apps.core.decorators import membership_required
from apps.work.selectors import visible_work_orders

from . import selectors, services
from .citations import segments
from .forms import FollowUpForm, QuestionForm
from .models import AssistantConversation, AssistantMessage

PAGE_SIZE = 25


def _require_reader(membership) -> None:
    if not (selectors.can_use(membership) or selectors.can_view_all(membership)):
        raise PermissionDenied


def _get_conversation(membership, pk) -> AssistantConversation:
    conversation = selectors.conversation_for(membership=membership, pk=pk)
    if conversation is None:
        raise Http404
    return conversation


def with_display(messages_list, photos=None) -> list:
    """Prepara cada mensaje para la plantilla: párrafos con sus fuentes, fotos y texto de error."""
    photos = photos or {}
    for message in messages_list:
        message.photos = photos.get(message.pk, [])
        message.segments = (
            segments(message.text, message.sources)
            if message.role == AssistantMessage.Role.ASSISTANT
            and message.status == AssistantMessage.Status.DONE
            else []
        )
        message.error_text = services.ERROR_MESSAGES.get(
            message.error_code, services.ERROR_MESSAGES["internal"]
        )
    return messages_list


def _thread_context(request, conversation, form=None) -> dict:
    """Mensajes, nota propuesta, marca de utilidad y caja de pregunta (también para el polling)."""
    membership = request.membership
    services.expire_stale_messages(conversation=conversation)
    turns = list(selectors.conversation_messages(conversation=conversation))
    with_display(turns, selectors.message_photos(messages=turns))
    pending = any(message.is_pending for message in turns) or (
        conversation.summary_status == AssistantConversation.SummaryStatus.PENDING
    )
    is_owner = conversation.started_by_id == membership.user_id and selectors.can_use(membership)
    is_open = conversation.status == AssistantConversation.Status.OPEN
    answered = any(
        m.role == AssistantMessage.Role.ASSISTANT and m.status == AssistantMessage.Status.DONE for m in turns
    )
    if is_owner and is_open and not pending and form is None:
        form = FollowUpForm()
    return {
        "conversation": conversation,
        "turns": turns,
        "pending": pending,
        "is_owner": is_owner,
        "is_open": is_open,
        "answered": answered,
        "configured": services.is_configured(),
        "form": form if is_owner and is_open else None,
        "helpful_choices": AssistantConversation.Helpful.choices,
        "summary_error": _summary_error(conversation),
    }


def _summary_error(conversation) -> str:
    if conversation.summary_status != AssistantConversation.SummaryStatus.ERROR:
        return ""
    last = (
        AssistantMessage.objects.filter(conversation=conversation, kind=AssistantMessage.Kind.SUMMARY)
        .order_by("-seq")
        .values_list("error_code", flat=True)
        .first()
    )
    return services.ERROR_MESSAGES.get(last or "", services.ERROR_MESSAGES["internal"])


# --- Lista --------------------------------------------------------------------------------


@membership_required
def conversation_list_view(request):
    """Mis conversaciones; el administrador puede ver las de toda la organización (AI-05 CA3)."""
    membership = request.membership
    _require_reader(membership)
    can_view_all = selectors.can_view_all(membership)
    show_all = can_view_all and request.GET.get("scope") == "all"
    conversations = selectors.conversation_list(membership=membership, mine_only=not show_all)
    page = Paginator(conversations, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "assistant/list.html",
        {
            "page": page,
            "show_all": show_all,
            "can_view_all": can_view_all,
            "can_use": selectors.can_use(membership),
        },
    )


# --- Pedir ayuda (AI-01) ------------------------------------------------------------------


@membership_required(perm=Perm.ASSISTANT_USE)
def conversation_start_view(request):
    """Primera pregunta sobre un activo (y su OT): `?asset=<id>&work_order=<id>`."""
    membership = request.membership
    asset = get_object_or_404(
        visible_assets(membership=membership).select_related("asset_class", "location"),
        pk=request.GET.get("asset") or None,
        is_active=True,
    )
    work_order = None
    if request.GET.get("work_order"):
        work_order = get_object_or_404(
            visible_work_orders(membership=membership), pk=request.GET["work_order"], asset=asset
        )
    configured = services.is_configured()
    quota = selectors.quota_status(organization=request.organization)
    form = QuestionForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            conversation = services.start_conversation(
                membership=membership,
                asset=asset,
                work_order=work_order,
                text=form.cleaned_data["text"],
                photo=form.cleaned_data.get("photo"),
                client_uuid=form.cleaned_data.get("client_uuid"),
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as exc:
            form.add_error(None, " ".join(exc.messages))
        else:
            return redirect("assistant_conversation", pk=conversation.pk)
    return render(
        request,
        "assistant/start.html",
        {
            "asset": asset,
            "work_order": work_order,
            "form": form,
            "configured": configured,
            "quota": quota,
            "recent": selectors.conversation_list(
                membership=membership, mine_only=True, asset=asset, work_order=work_order
            )[:3],
        },
        status=400 if form.errors else 200,
    )


# --- Conversación -------------------------------------------------------------------------


@membership_required
def conversation_detail_view(request, pk):
    membership = request.membership
    _require_reader(membership)
    conversation = _get_conversation(membership, pk)
    return render(request, "assistant/detail.html", _thread_context(request, conversation))


@membership_required
def conversation_thread_view(request, pk):
    """Fragmento que htmx vuelve a pedir cada ~2 s mientras haya una respuesta pendiente."""
    membership = request.membership
    _require_reader(membership)
    conversation = _get_conversation(membership, pk)
    return render(request, "assistant/_thread.html", _thread_context(request, conversation))


def _owner_action(request, pk, action, success=""):
    membership = request.membership
    _require_reader(membership)
    conversation = _get_conversation(membership, pk)
    try:
        action(membership=membership, conversation=conversation)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if success:
            messages.success(request, success)
    return redirect("assistant_conversation", pk=pk)


@require_POST
@membership_required(perm=Perm.ASSISTANT_USE)
def conversation_ask_view(request, pk):
    membership = request.membership
    conversation = _get_conversation(membership, pk)
    form = FollowUpForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            services.ask(
                membership=membership,
                conversation=conversation,
                text=form.cleaned_data["text"],
                photo=form.cleaned_data.get("photo"),
                client_uuid=form.cleaned_data.get("client_uuid"),
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as exc:
            form.add_error(None, " ".join(exc.messages))
        else:
            return redirect(reverse("assistant_conversation", args=[pk]) + "#composer")
    return render(
        request, "assistant/detail.html", _thread_context(request, conversation, form=form), status=400
    )


@require_POST
@membership_required(perm=Perm.ASSISTANT_USE)
def conversation_summary_view(request, pk):
    """Proponer nota de cierre (AI-04 CA1)."""
    return _owner_action(request, pk, services.propose_summary)


@require_POST
@membership_required(perm=Perm.ASSISTANT_USE)
def conversation_helpful_view(request, pk):
    """¿Te sirvió? sí / en parte / no (AI-04 CA2)."""
    helpful = request.POST.get("helpful", "")

    def mark(*, membership, conversation):
        services.mark_helpful(membership=membership, conversation=conversation, helpful=helpful)

    return _owner_action(request, pk, mark, success=_("Gracias, quedó registrado si te sirvió."))


@require_POST
@membership_required(perm=Perm.ASSISTANT_USE)
def conversation_close_view(request, pk):
    return _owner_action(request, pk, services.close_conversation, success=_("Conversación cerrada."))
