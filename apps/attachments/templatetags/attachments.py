from django import template

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
