from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.organizations.models import Organization

from . import services


@receiver(post_save, sender=Organization, dispatch_uid="work_seed_failure_catalog")
def seed_failure_catalog_for_new_organization(sender, instance, created, **kwargs):
    """Cada organización nueva recibe el catálogo base de falla (REQ-03 CA1)."""
    if created:
        services.seed_default_failure_catalog(organization=instance)
