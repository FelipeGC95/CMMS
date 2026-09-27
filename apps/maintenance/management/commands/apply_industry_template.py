"""Aplica una plantilla de rubro a una organización (uso de plataforma, ORG-03 CA3)."""

from django.core.management.base import BaseCommand, CommandError

from apps.maintenance.industry_templates import PARTS, apply_industry_template, preview_industry_template
from apps.maintenance.templates_library import INDUSTRIES
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Copia a una organización las clases de activo, el catálogo de fallas y los planes de un rubro. "
        "Idempotente: omite lo que ya existe (por código) y nunca sobrescribe."
    )

    def add_arguments(self, parser):
        parser.add_argument("organization", help="slug de la organización")
        parser.add_argument("industry", choices=INDUSTRIES)
        parser.add_argument(
            "--parts", nargs="+", choices=PARTS, default=list(PARTS), help="qué copiar (por defecto, todo)"
        )
        parser.add_argument("--dry-run", action="store_true", help="solo muestra lo que se crearía")

    def handle(self, *args, organization, industry, parts, dry_run, **options):
        # Comando de plataforma: el operador elige la organización por su slug.
        # Organization no es un modelo por organización (no usa `all_tenants`);
        # todo lo que se escribe queda dentro de `tenant_context(org)` en el servicio.
        org = Organization.objects.filter(slug=organization).first()
        if org is None:
            raise CommandError(f"No existe la organización «{organization}».")
        run = preview_industry_template if dry_run else apply_industry_template
        summary = run(organization=org, industry=industry, parts=tuple(parts))
        verb = "Se crearían" if dry_run else "Creados"
        self.stdout.write(
            f"{org}: plantilla «{summary.name}» v{summary.version}"
            + (f" ({summary.review_note})" if summary.is_draft else "")
        )
        self.stdout.write(
            f"{verb}: {len(summary.classes)} clases ({summary.attributes} atributos), "
            f"{len(summary.failure_codes)} códigos de falla, {len(summary.plans)} planes."
        )
        skipped = (
            len(summary.classes_skipped) + len(summary.failure_codes_skipped) + len(summary.plans_skipped)
        )
        if skipped:
            self.stdout.write(f"Omitidos por existir ya: {skipped}.")
        self.stdout.write(self.style.SUCCESS("Listo." if not dry_run else "Sin cambios (--dry-run)."))
