"""Alcance por sitio (ORG-06): cálculo de ubicaciones permitidas."""

from apps.accounts.models import Membership, MembershipLocation
from apps.accounts.scoping import allowed_location_ids
from apps.organizations.models import Location


def _tree():
    site = Location.objects.create(code="S1", name="Planta 1")
    area = Location.objects.create(code="A1", name="Área 1", kind="area", parent=site)
    line = Location.objects.create(code="L1", name="Línea 1", kind="line", parent=area)
    other = Location.objects.create(code="S2", name="Planta 2")
    return site, area, line, other


def test_unrestricted_membership_sees_everything(in_a):
    assert allowed_location_ids(in_a.tech) is None


def test_restricted_membership_includes_descendants(in_a):
    site, area, line, other = _tree()
    in_a.tech.restricted_to_locations = True
    MembershipLocation.objects.create(membership=in_a.tech, location=area)
    assert allowed_location_ids(in_a.tech) == {area.id, line.id}


def test_external_is_always_scoped_and_sees_nothing_without_locations(in_a):
    _tree()
    external = Membership(user=in_a.viewer.user, organization=in_a.org, role=Membership.Role.EXTERNAL)
    assert allowed_location_ids(external) == frozenset()


def test_no_membership_sees_nothing(db):
    assert allowed_location_ids(None) == frozenset()


def test_result_is_cached_per_membership(in_a, django_assert_num_queries):
    site, *_ = _tree()
    in_a.tech.restricted_to_locations = True
    MembershipLocation.objects.create(membership=in_a.tech, location=site)
    with django_assert_num_queries(2):
        allowed_location_ids(in_a.tech)
        allowed_location_ids(in_a.tech)
