from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.decorators import membership_required
from apps.core.middleware import SESSION_KEY
from apps.organizations.models import Location

from . import services
from .forms import AcceptInvitationForm, InviteForm, MemberForm
from .models import Invitation, Membership, User
from .permissions import Perm
from .selectors import invitation_by_token, pending_invitations, team_member, team_members


@login_required
@require_POST
def switch_organization(request):
    services.switch_organization(request=request, organization_id=request.POST.get("organization"))
    # Siempre al inicio: la página anterior es de la otra organización.
    return redirect("dashboard")


# ---------- Equipo (ORG-05/06) ----------


@membership_required(perm=Perm.TEAM_MANAGE)
def team_view(request):
    form = InviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            invitation = services.invite_member(
                membership=request.membership,
                email=form.cleaned_data["email"],
                role=form.cleaned_data["role"],
                base_url=request.build_absolute_uri("/"),
            )
        except services.TeamError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, _("Invitación enviada a %(email)s.") % {"email": invitation.email})
            return redirect("team")
    page = Paginator(team_members(membership=request.membership), 25).get_page(request.GET.get("page"))
    return render(
        request,
        "accounts/team.html",
        {"form": form, "page": page, "invitations": pending_invitations(membership=request.membership)[:50]},
    )


@membership_required(perm=Perm.TEAM_MANAGE)
@require_POST
def invitation_revoke_view(request, pk):
    try:
        invitation = services.revoke_invitation(membership=request.membership, invitation_id=pk)
    except Invitation.DoesNotExist as error:
        raise Http404 from error
    messages.success(request, _("Invitación a %(email)s revocada.") % {"email": invitation.email})
    return redirect("team")


def _get_member_or_404(request, pk):
    try:
        return team_member(membership=request.membership, pk=pk)
    except Membership.DoesNotExist as error:
        raise Http404 from error


@membership_required(perm=Perm.TEAM_MANAGE)
def member_edit_view(request, pk):
    target = _get_member_or_404(request, pk)
    locations = Location.objects.filter(is_active=True).select_related("parent").order_by("code")
    initial = {
        "role": target.role,
        "restricted_to_locations": target.restricted_to_locations,
        "locations": [scope.location_id for scope in target.scope.all()],
    }
    form = MemberForm(request.POST or None, initial=initial, locations=locations)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_member(
                membership=request.membership,
                target=target,
                role=form.cleaned_data["role"],
                restricted_to_locations=form.cleaned_data["restricted_to_locations"],
                locations=form.cleaned_data["locations"],
            )
        except services.TeamError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, _("Cambios guardados."))
            return redirect("team")
    return render(request, "accounts/member_form.html", {"form": form, "target": target})


@membership_required(perm=Perm.TEAM_MANAGE)
@require_POST
def member_active_view(request, pk):
    target = _get_member_or_404(request, pk)
    active = request.POST.get("active") == "1"
    try:
        services.set_member_active(membership=request.membership, target=target, active=active)
    except services.TeamError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, _("Miembro reactivado.") if active else _("Miembro desactivado."))
    return redirect("team")


# ---------- Aceptar una invitación (enlace del correo) ----------


def _enter_organization(request, organization):
    request.session[SESSION_KEY] = str(organization.pk)
    messages.success(request, _("Te uniste a %(org)s.") % {"org": organization.name})
    return redirect("dashboard")


def invitation_accept_view(request, token):
    invitation = invitation_by_token(token)
    if invitation is None:
        return render(request, "accounts/invitation_invalid.html", status=404)
    context = {"invitation": invitation, "organization": invitation.organization}

    if request.user.is_authenticated:
        if request.user.email.lower() != invitation.email.lower():
            return render(request, "accounts/invitation_accept.html", {**context, "wrong_user": True})
        if request.method == "POST":
            try:
                services.accept_invitation(invitation=invitation, user=request.user)
            except services.TeamError as error:
                messages.error(request, str(error))
                return redirect("dashboard")
            return _enter_organization(request, invitation.organization)
        return render(request, "accounts/invitation_accept.html", context)

    # Con cuenta previa se ingresa primero y se vuelve a este enlace (ORG-05 CA2).
    if User.objects.filter(email__iexact=invitation.email).exists():
        messages.info(
            request, _("Ingresa con %(email)s para aceptar la invitación.") % {"email": invitation.email}
        )
        return redirect(f"{reverse('account_login')}?next={request.path}")

    form = AcceptInvitationForm(request.POST or None, email=invitation.email)
    if request.method == "POST" and form.is_valid():
        try:
            user = services.register_from_invitation(
                invitation=invitation,
                full_name=form.cleaned_data["full_name"],
                password=form.cleaned_data["password1"],
            )
        except services.TeamError as error:
            form.add_error(None, str(error))
        else:
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return _enter_organization(request, invitation.organization)
    return render(request, "accounts/invitation_accept.html", {**context, "form": form})
