from django import template

from apps.accounts.permissions import Perm
from apps.billing.access import can_act

register = template.Library()


@register.filter
def can_import_assets(membership) -> bool:
    return can_act(membership, Perm.ASSET_MANAGE)
