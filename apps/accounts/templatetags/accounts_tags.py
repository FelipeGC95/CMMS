from django import template

from apps.accounts.permissions import Perm, has_perm
from apps.organizations.modules import has_module as plan_has_module

register = template.Library()


@register.filter
def can(membership, perm):
    """`{% if membership|can:"team.manage" %}`: la plantilla consulta la misma matriz de permisos."""
    return has_perm(membership, Perm(perm))


@register.filter
def has_module(organization, module):
    """`{% if organization|has_module:"api" %}`: oculta lo que el plan no incluye (ORG-10 CA2)."""
    return plan_has_module(organization, module)
