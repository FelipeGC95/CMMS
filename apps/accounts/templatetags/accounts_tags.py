from django import template

from apps.accounts.permissions import Perm, has_perm
from apps.billing.access import can_act
from apps.organizations.modules import has_module as plan_has_module

register = template.Library()


@register.filter
def can(membership, perm):
    """`{% if membership|can:"team.manage" %}`: ¿puede hacerlo ahora? Para botones y formularios.

    Consulta la misma matriz de permisos y, en solo lectura por facturación (ORG-13), niega los
    permisos de escritura (`billing.access.can_act`).
    """
    return can_act(membership, Perm(perm))


@register.filter
def can_view(membership, perm):
    """`{% if membership|can_view:"plan.manage" %}`: el rol tiene el permiso aunque la organización
    esté en solo lectura. Para enlaces a páginas que se pueden consultar (listas, fichas)."""
    return has_perm(membership, Perm(perm))


@register.filter
def has_module(organization, module):
    """`{% if organization|has_module:"api" %}`: oculta lo que el plan no incluye (ORG-10 CA2)."""
    return plan_has_module(organization, module)
