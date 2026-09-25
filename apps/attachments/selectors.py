from django.db.models import Sum

from .models import Attachment


def attachments_for(*, target_type: str, target_id):
    return Attachment.objects.filter(target_type=target_type, target_id=target_id).select_related(
        "uploaded_by"
    )


def storage_bytes_used(*, organization) -> int:
    """Bytes usados por la organización (AST-06 CA2); el cupo se hace cumplir en ORG-10."""
    total = Attachment.all_tenants.filter(organization=organization).aggregate(total=Sum("size_bytes"))[
        "total"
    ]
    return total or 0
