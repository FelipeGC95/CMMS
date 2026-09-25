from django import template

from apps.accounts.permissions import Perm, has_perm

register = template.Library()


@register.filter
def can(membership, perm):
    """`{% if membership|can:"team.manage" %}`: la plantilla consulta la misma matriz de permisos."""
    return has_perm(membership, Perm(perm))
