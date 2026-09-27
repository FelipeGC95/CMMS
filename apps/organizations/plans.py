"""Catálogo inicial de planes comerciales (lo usan la migración y seed_demo)."""

# Cupos provisionales: se ajustan en el admin cuando se definan los precios.
# Módulos por código (ORG-10): se hacen cumplir en apps/organizations/modules.py.
BASE_MODULES = ["assets", "work_orders", "preventive", "requests", "kpis"]
DEFAULT_PLANS = [
    # code, name, max_assets, max_users, max_storage_mb, módulos extra
    ("trial", "Prueba", 50, 10, 1024, []),
    ("basic", "Básico", 50, 10, 2048, []),
    ("pro", "Profesional", 300, None, 20480, ["api", "exports"]),
    ("enterprise", "Empresa", None, None, None, ["api", "exports", "sso"]),
]


def ensure_default_plans(plan_model):
    """Crea los planes que falten sin tocar los ya editados. Recibe el modelo (sirve en migraciones)."""
    for code, name, max_assets, max_users, max_storage_mb, extra in DEFAULT_PLANS:
        plan_model.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "max_assets": max_assets,
                "max_users": max_users,
                "max_storage_mb": max_storage_mb,
                "features": BASE_MODULES + extra,
            },
        )
