"""Catálogo inicial de planes comerciales (lo usan la migración y seed_demo)."""

# Cupos provisionales: se ajustan en el admin cuando se definan los precios.
# Módulos por código (ORG-10): se hacen cumplir en apps/organizations/modules.py.
BASE_MODULES = ["assets", "work_orders", "preventive", "requests", "kpis"]
DEFAULT_PLANS = [
    # code, name, max_assets, max_users, max_storage_mb, módulos extra
    ("trial", "Prueba", 50, 10, 1024, ["inventory", "condition"]),
    ("basic", "Básico", 50, 10, 2048, []),
    ("pro", "Profesional", 300, None, 20480, ["api", "exports", "inventory", "condition", "assistant"]),
    (
        "enterprise",
        "Empresa",
        None,
        None,
        None,
        ["api", "exports", "sso", "inventory", "condition", "assistant"],
    ),
]
# Planes que incluyen inventario (INV-16, decisión Q4): la migración de datos lo agrega a los existentes.
INVENTORY_PLAN_CODES = ("trial", "pro", "enterprise")
# Planes con mantenimiento por condición (CBM-08, D4): los mismos que inventario.
CONDITION_PLAN_CODES = ("trial", "pro", "enterprise")
# Asistente de diagnóstico con IA (AI-05, D2): solo Profesional y Empresa, con cupo mensual de
# conversaciones por organización (editable en el admin; en blanco = sin límite).
ASSISTANT_PLAN_CODES = ("pro", "enterprise")
ASSISTANT_CONVERSATION_QUOTAS = {"pro": 100, "enterprise": 500}


def ensure_default_plans(plan_model):
    """Crea los planes que falten sin tocar los ya editados. Recibe el modelo (sirve en migraciones).

    El cupo del asistente solo se fija si el modelo (quizá histórico) ya tiene el campo.
    """
    has_quota = any(field.name == "max_assistant_conversations" for field in plan_model._meta.get_fields())
    for code, name, max_assets, max_users, max_storage_mb, extra in DEFAULT_PLANS:
        defaults = {
            "name": name,
            "max_assets": max_assets,
            "max_users": max_users,
            "max_storage_mb": max_storage_mb,
            "features": BASE_MODULES + extra,
        }
        if has_quota:
            defaults["max_assistant_conversations"] = ASSISTANT_CONVERSATION_QUOTAS.get(code)
        plan_model.objects.get_or_create(code=code, defaults=defaults)
