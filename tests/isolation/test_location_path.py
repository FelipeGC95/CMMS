"""La ruta guardada de las ubicaciones solo se recalcula dentro de la organización."""

from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.organizations.services import update_location


def test_renaming_a_location_does_not_touch_other_organizations(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        foreign_site = Location.objects.create(code="P1", name="Planta")
        foreign_area = Location.objects.create(code="A1", name="Área", parent=foreign_site)
    with tenant_context(tenant_a.org):
        site = Location.objects.create(code="P1", name="Planta")
        Location.objects.create(code="A1", name="Área", parent=site)
        update_location(
            membership=tenant_a.admin, location=site, code="P1", name="Renombrada", kind="site", parent=None
        )
        assert str(Location.objects.get(code="A1")) == "Renombrada › Área"
    with tenant_context(tenant_b.org):
        assert str(Location.objects.get(pk=foreign_area.pk)) == "Planta › Área"
