from django import template

from apps.attachments.forms import ManualForm
from apps.attachments.selectors import manuals_for_asset, manuals_for_class
from apps.attachments.selectors import storage_usage as storage_usage_selector

register = template.Library()


@register.simple_tag(takes_context=True)
def storage_usage(context):
    """Uso del cupo de almacenamiento de la organización activa (AST-06 CA2), en una consulta."""
    request = context.get("request")
    organization = getattr(request, "organization", None)
    if organization is None:
        return None
    return storage_usage_selector(organization=organization)


@register.simple_tag
def manuals_for(target):
    """Manuales de un activo (propios + de su clase) o de una clase (AST-06 CA3), en una consulta."""
    from apps.assets.models import Asset

    if isinstance(target, Asset):
        return list(manuals_for_asset(asset=target))
    return list(manuals_for_class(asset_class=target))


@register.simple_tag
def manual_form():
    return ManualForm()
