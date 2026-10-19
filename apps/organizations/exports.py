"""Armado del ZIP de exportación completa (X-03, NF-07/NF-08).

Un CSV por tabla de negocio (cada `TenantModel` concreto, descubierto en tiempo
de ejecución, sin listas a mano), más la organización, sus membresías e
invitaciones, y todos los archivos (adjuntos y logo) bajo `archivos/`. Los CSV
van en UTF-8 con BOM para que Excel los abra con tildes, y separador coma.

Se ejecuta siempre dentro de `tenant_context(organización)`: además del filtro
explícito por `organization_id`, en PostgreSQL la política RLS (ADR-026)
impide que una fila de otra organización llegue al archivo.
"""

import csv
import datetime
import decimal
import io
import json
import re
import tempfile
import uuid
import zipfile

from django.apps import apps
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.services import is_sensitive
from apps.core.models import TenantModel
from apps.core.tenancy import require_current_organization

# Tablas que no se exportan: la propia exportación y el texto derivado de los
# manuales (el PDF original va en archivos/).
EXCLUDED_MODELS = {"organizations.dataexport", "attachments.documentpage"}
CHUNK_SIZE = 2000
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def exportable_models() -> list:
    return sorted(
        (
            model
            for model in apps.get_models()
            if issubclass(model, TenantModel)
            and model._meta.managed
            and not model._meta.proxy
            and model._meta.label_lower not in EXCLUDED_MODELS
        ),
        key=lambda model: model._meta.label_lower,
    )


def exported_fields(model) -> list:
    """Campos concretos salvo secretos (hash de tokens y similares)."""
    return [field for field in model._meta.concrete_fields if not is_sensitive(field.name)]


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, decimal.Decimal | uuid.UUID):
        return str(value)
    return value


def _write_csv(archive, name, header, rows) -> int:
    """Escribe un CSV al ZIP en streaming; devuelve la cantidad de filas."""
    count = 0
    with archive.open(name, "w", force_zip64=True) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        writer = csv.writer(text)
        writer.writerow(header)
        for row in rows:
            writer.writerow([_cell(value) for value in row])
            count += 1
        text.flush()
        text.detach()
    return count


def _add_file(archive, fieldfile, arcname) -> bool:
    try:
        with (
            fieldfile.storage.open(fieldfile.name, "rb") as source,
            archive.open(arcname, "w", force_zip64=True) as target,
        ):
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
    except (FileNotFoundError, OSError):
        return False
    return True


def build_archive() -> tuple[tempfile.SpooledTemporaryFile, list[str]]:
    """Arma el ZIP de la organización del contexto en un archivo temporal (memoria acotada).

    Devuelve el archivo (posicionado al inicio) y las líneas del README.
    """
    from apps.accounts.models import Invitation, Membership
    from apps.attachments.models import Attachment

    organization = require_current_organization()
    target = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    summary = []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        org_fields = [
            f for f in exported_fields(type(organization)) if f.name not in {"purge_summary", "deleted_at"}
        ]
        _write_csv(
            archive,
            "datos/organizacion.csv",
            [f.attname for f in org_fields],
            [[f.value_from_object(organization) for f in org_fields]],
        )
        summary.append(("datos/organizacion.csv", str(organization._meta.verbose_name), 1))

        memberships = (
            Membership.objects.filter(organization=organization)
            .order_by("created_at")
            .values_list(
                "id",
                "user_id",
                "user__email",
                "user__full_name",
                "role",
                "custom_role_id",
                "is_active",
                "created_at",
            )
        )
        header = ["id", "user_id", "email", "full_name", "role", "custom_role_id", "is_active", "created_at"]
        count = _write_csv(archive, "datos/membresias.csv", header, memberships.iterator())
        summary.append(("datos/membresias.csv", str(Membership._meta.verbose_name_plural), count))

        invitation_fields = exported_fields(Invitation)
        invitations = (
            Invitation.objects.filter(organization=organization)
            .order_by("created_at")
            .values_list(*[f.attname for f in invitation_fields])
        )
        count = _write_csv(
            archive,
            "datos/invitaciones.csv",
            [f.attname for f in invitation_fields],
            invitations.iterator(),
        )
        summary.append(("datos/invitaciones.csv", str(Invitation._meta.verbose_name_plural), count))

        for model in exportable_models():
            fields = exported_fields(model)
            columns = [f.attname for f in fields]
            # Filtro explícito + RLS en PostgreSQL: nunca filas de otra organización.
            rows = (
                model._base_manager.filter(organization_id=organization.pk)
                .order_by("pk")
                .values_list(*columns)
                .iterator(chunk_size=CHUNK_SIZE)
            )
            name = f"datos/{model._meta.app_label}_{model._meta.model_name}.csv"
            count = _write_csv(archive, name, columns, rows)
            summary.append((name, str(model._meta.verbose_name_plural), count))

        missing = []
        files = 0
        attachments = Attachment._base_manager.filter(organization_id=organization.pk).order_by("pk")
        for attachment in attachments.iterator(chunk_size=CHUNK_SIZE):
            filename = SAFE_NAME.sub("_", attachment.filename)[:120] or "archivo"
            arcname = (
                f"archivos/adjuntos/{attachment.target_type}/{attachment.purpose}/{attachment.pk}_{filename}"
            )
            if _add_file(archive, attachment.file, arcname):
                files += 1
            else:
                missing.append(attachment.file.name)
        if organization.logo:
            if _add_file(archive, organization.logo, "archivos/logo.png"):
                files += 1
            else:
                missing.append(organization.logo.name)

        readme = _readme(organization, summary, files, missing)
        archive.writestr("LEEME.txt", readme.encode("utf-8"))
    target.seek(0)
    return target, readme


def _readme(organization, summary, files, missing) -> str:
    lines = [
        _("Exportación completa de datos · %(name)s") % {"name": organization.name},
        _("Organización: %(id)s") % {"id": organization.pk},
        _("Generada: %(date)s (UTC)") % {"date": timezone.now().isoformat(timespec="seconds")},
        "",
        _(
            "datos/: un archivo CSV por tabla (UTF-8 con BOM, separador coma, primera fila = columnas). "
            "Las fechas van en ISO 8601 y UTC. Las columnas *_id enlazan con la columna id de otra "
            "tabla; user_id enlaza con datos/membresias.csv."
        ),
        _(
            "archivos/: adjuntos (fotos, documentos, firmas, manuales) por tipo de objeto y propósito, "
            "con el nombre <id del adjunto>_<nombre original> (ver datos/attachments_attachment.csv); "
            "logo.png es el logo de la organización."
        ),
        _("No se incluyen secretos (hash de tokens de API o de invitaciones)."),
        "",
    ]
    lines += [
        _("%(file)s · %(label)s · %(count)s filas") % {"file": name, "label": label, "count": count}
        for name, label, count in summary
    ]
    lines += ["", _("Archivos incluidos: %(count)s") % {"count": files}]
    if missing:
        lines += [_("No se encontraron en el almacenamiento:"), *[f"  {name}" for name in missing]]
    return "\n".join(lines) + "\n"
