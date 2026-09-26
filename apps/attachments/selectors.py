from dataclasses import dataclass

from django.db.models import OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from apps.organizations.models import Organization

from .models import Attachment

BYTES_PER_MB = 1024 * 1024
STORAGE_WARNING_RATIO = 0.8  # ORG-10 CA1: aviso al 80 % del cupo.


def attachments_for(*, target_type: str, target_id):
    return Attachment.objects.filter(target_type=target_type, target_id=target_id).select_related(
        "uploaded_by"
    )


def attachments_by_target(*, target_type: str, target_ids) -> dict:
    """Adjuntos de varios objetos en una sola consulta: {target_id: [adjuntos]}."""
    grouped: dict = {}
    for attachment in Attachment.objects.filter(target_type=target_type, target_id__in=list(target_ids)):
        grouped.setdefault(attachment.target_id, []).append(attachment)
    return grouped


def storage_bytes_used(*, organization) -> int:
    """Bytes usados por la organización (AST-06 CA2), medidos después de comprimir las imágenes."""
    # all_tenants: se filtra explícitamente por la organización recibida.
    total = Attachment.all_tenants.filter(organization=organization).aggregate(total=Sum("size_bytes"))[
        "total"
    ]
    return total or 0


@dataclass(frozen=True)
class StorageUsage:
    used_bytes: int
    quota_bytes: int | None  # None = plan sin cupo (ilimitado)

    @property
    def used_mb(self) -> float:
        return self.used_bytes / BYTES_PER_MB

    @property
    def quota_mb(self) -> int | None:
        return None if self.quota_bytes is None else self.quota_bytes // BYTES_PER_MB

    @property
    def ratio(self) -> float:
        if not self.quota_bytes:
            return 0.0 if self.quota_bytes is None else 1.0
        return self.used_bytes / self.quota_bytes

    @property
    def is_warning(self) -> bool:
        return self.quota_bytes is not None and self.ratio >= STORAGE_WARNING_RATIO

    @property
    def is_full(self) -> bool:
        return self.quota_bytes is not None and self.used_bytes >= self.quota_bytes

    def allows(self, extra_bytes: int) -> bool:
        return self.quota_bytes is None or self.used_bytes + extra_bytes <= self.quota_bytes


def storage_usage(*, organization) -> StorageUsage:
    """Uso y cupo de almacenamiento del plan en una sola consulta (AST-06 CA2, ORG-10)."""
    # all_tenants: el filtro por organización es explícito (OuterRef) y no depende del contexto.
    used = (
        Attachment.all_tenants.filter(organization=OuterRef("pk"))
        .values("organization")
        .annotate(total=Sum("size_bytes"))
        .values("total")
    )
    quota_mb, used_bytes = (
        Organization.objects.filter(pk=organization.pk)
        .annotate(used=Coalesce(Subquery(used), 0))
        .values_list("plan__max_storage_mb", "used")
        .get()
    )
    return StorageUsage(
        used_bytes=used_bytes,
        quota_bytes=None if quota_mb is None else quota_mb * BYTES_PER_MB,
    )
