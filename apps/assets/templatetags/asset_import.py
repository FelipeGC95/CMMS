from django import template

from apps.accounts.permissions import Perm, has_perm

register = template.Library()


@register.filter
def can_import_assets(membership) -> bool:
    return has_perm(membership, Perm.ASSET_MANAGE)
