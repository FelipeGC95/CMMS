"""Row-Level Security en PostgreSQL (NF-07, ADR-026).

Solo en PostgreSQL (el CI): SQLite no tiene RLS. Las pruebas usan SQL crudo,
que no pasa por el manager de TenantModel: lo único que aísla es la política.
"""

import pytest
from django.db import DatabaseError, connection, transaction

from apps.assets.models import Asset, AssetClass
from apps.core import rls
from apps.core.models import PlatformQuerySetMixin, TenantModel
from apps.core.tenancy import platform_context, tenant_context

pytestmark = pytest.mark.skipif(connection.vendor != "postgresql", reason="RLS solo existe en PostgreSQL")

ASSET_TABLE = Asset._meta.db_table


def _org_ids_visible(table=ASSET_TABLE):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT DISTINCT organization_id FROM {connection.ops.quote_name(table)}")
        return {row[0] for row in cursor.fetchall()}


def _setting(name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [name])
        return cursor.fetchone()[0]


@pytest.mark.django_db
def test_app_role_is_subject_to_rls():
    # Un superusuario o un rol BYPASSRLS ignora las políticas: los tests no probarían nada.
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cursor.fetchone() == (False, False)


@pytest.mark.django_db
def test_every_tenant_table_has_forced_policy():
    assert ASSET_TABLE in rls.tenant_tables()
    assert rls.missing_policies() == []


def test_every_tenant_model_all_tenants_bypasses_rls():
    from django.apps import apps

    for model in apps.get_models():
        if issubclass(model, TenantModel):
            assert isinstance(model.all_tenants.all(), PlatformQuerySetMixin), model


def test_raw_sql_without_context_sees_no_rows(tenant_a, tenant_b):
    assert _org_ids_visible() == set()


def test_context_hides_other_tenant_rows(tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        assert _org_ids_visible() == {tenant_a.org.pk}
    with tenant_context(tenant_b.org):
        assert _org_ids_visible() == {tenant_b.org.pk}


def test_cannot_write_rows_of_other_tenant(tenant_a, tenant_b):
    table = connection.ops.quote_name(AssetClass._meta.db_table)
    with tenant_context(tenant_a.org):
        # bulk_create no pasa por TenantModel.save: solo la política lo detiene.
        with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
            AssetClass.objects.bulk_create([AssetClass(organization=tenant_b.org, code="X", name="X")])
        # Mover una fila propia a otra organización tampoco pasa el WITH CHECK.
        with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE {table} SET organization_id = %s", [tenant_b.org.pk])
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET name = 'hack' WHERE organization_id = %s", [tenant_b.org.pk])
            assert cursor.rowcount == 0


def test_setting_is_reset_when_context_exits(tenant_a):
    with tenant_context(tenant_a.org):
        assert _setting("app.current_organization") == str(tenant_a.org.pk)
    assert _setting("app.current_organization") in ("", None)


def test_nested_contexts_restore_outer_value(tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        with tenant_context(tenant_b.org):
            assert _setting("app.current_organization") == str(tenant_b.org.pk)
        assert _setting("app.current_organization") == str(tenant_a.org.pk)


def test_setting_is_reset_after_request(client, tenant_a):
    client.force_login(tenant_a.admin.user)
    response = client.get("/")
    assert response.status_code in (200, 302)
    assert _setting("app.current_organization") in ("", None)
    assert _setting("app.bypass_rls") in ("", "off", None)


def test_platform_context_sees_everything_and_resets(tenant_a, tenant_b):
    with platform_context():
        assert _org_ids_visible() == {tenant_a.org.pk, tenant_b.org.pk}
    assert _setting("app.bypass_rls") in ("", "off", None)
    assert _org_ids_visible() == set()


def test_all_tenants_still_sees_every_tenant(tenant_a, tenant_b):
    assert Asset.all_tenants.count() == 2
    assert {a.organization_id for a in Asset.all_tenants.all()} == {tenant_a.org.pk, tenant_b.org.pk}
    with tenant_context(tenant_a.org):
        assert Asset.all_tenants.filter(organization=tenant_b.org).exists()
        assert Asset.objects.count() == 1
    assert _setting("app.bypass_rls") in ("", "off", None)
