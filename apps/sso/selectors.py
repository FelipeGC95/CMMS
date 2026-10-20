"""Lecturas del SSO (ORG-11, ADR-032).

Las de la pantalla de configuración reciben la membresía y exigen `organization.manage`.
Las del ingreso (`sso_for_email`, `password_login_blocked`) corren antes de conocer la
organización: buscan el dominio en toda la plataforma con `all_tenants` (cada una lo
justifica) y devuelven solo la organización dueña del dominio verificado.
"""

from dataclasses import dataclass

from django.shortcuts import get_object_or_404

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, require_perm
from apps.organizations.models import Organization
from apps.organizations.modules import has_module

from .models import SsoConfiguration, SsoDomain

MODULE = "sso"


def email_domain(email: str) -> str:
    _, _, domain = (email or "").strip().rpartition("@")
    return domain.rstrip(".").lower()


# --- Pantalla de configuración ------------------------------------------------------------


def get_configuration(*, membership) -> SsoConfiguration | None:
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    return SsoConfiguration.objects.select_related("default_custom_role").first()


def domain_list(*, membership):
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    return SsoDomain.objects.order_by("domain")


def get_domain(*, membership, pk) -> SsoDomain:
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    return get_object_or_404(SsoDomain, pk=pk)


# --- Ingreso (antes del contexto de tenant) -----------------------------------------------


@dataclass(frozen=True)
class SsoTarget:
    organization: Organization
    configuration: SsoConfiguration


def sso_for_email(email: str) -> SsoTarget | None:
    """Organización cuyo SSO atiende este correo: dominio verificado, configuración activa,
    organización activa y plan con el módulo `sso`. Si falta algo, None (se entra con clave)."""
    domain = email_domain(email)
    if not domain:
        return None
    # all_tenants: el dominio se busca antes de saber la organización (login). El índice único
    # parcial garantiza un solo dominio verificado en toda la plataforma.
    found = (
        SsoDomain.all_tenants.filter(domain=domain, verified_at__isnull=False)
        .select_related("organization__plan")
        .first()
    )
    if found is None or not found.organization.is_active or not has_module(found.organization, MODULE):
        return None
    # all_tenants: misma búsqueda previa al contexto, acotada a la organización del dominio.
    configuration = SsoConfiguration.all_tenants.filter(
        organization_id=found.organization_id, is_active=True
    ).first()
    if configuration is None:
        return None
    return SsoTarget(found.organization, configuration)


def password_login_blocked(user) -> Organization | None:
    """Organización que exige SSO al correo de `user`, o None si puede entrar con contraseña.

    La regla la define el dominio del correo, no la organización activa (ORG-11 CA7). Los
    administradores de esa organización conservan la contraseña como acceso de emergencia.
    """
    target = sso_for_email(getattr(user, "email", ""))
    if target is None or not target.configuration.enforce_sso:
        return None
    is_admin = Membership.objects.filter(
        user=user, organization=target.organization, role=Membership.Role.ADMIN, is_active=True
    ).exists()
    return None if is_admin else target.organization


def enforced_domain(email: str) -> bool:
    """¿El dominio del correo exige SSO? (sin mirar quién es: no revela cuentas)."""
    target = sso_for_email(email)
    return target is not None and target.configuration.enforce_sso
