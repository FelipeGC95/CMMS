"""Módulos por plan (ORG-10 CA2): qué URL y qué endpoint de la API pertenece a cada módulo.

`Plan.features` lista los códigos de módulo incluidos (`plans.BASE_MODULES` y extras
como `api` o `exports`). Un módulo no incluido desaparece de la navegación
(filtro `has_module` en las plantillas) y sus URL responden 403 con un mensaje de
plan: la web por `PlanModuleMiddleware` y la API por el permiso `PlanIncludesModule`.
Una organización sin plan, o con un plan sin módulos listados (como un cupo en blanco),
no tiene restricciones de módulos.

El inicio (`/`, con los KPI básicos) no se bloquea: es la portada de todos los planes. Los
indicadores ampliados (`/reports/`, KPI-02) sí pertenecen al módulo `kpis`.
"""

import re
from dataclasses import dataclass

from django.shortcuts import render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from rest_framework import permissions

from apps.accounts.permissions import PermissionDeniedError

MODULE_LABELS = {
    "assets": gettext_lazy("Activos"),
    "work_orders": gettext_lazy("Órdenes de trabajo"),
    "preventive": gettext_lazy("Mantenimiento preventivo"),
    "requests": gettext_lazy("Solicitudes"),
    "kpis": gettext_lazy("Indicadores"),
    "api": gettext_lazy("API"),
    "exports": gettext_lazy("Exportación a Excel y PDF"),
    "sso": gettext_lazy("Inicio de sesión único (SSO)"),
    "inventory": gettext_lazy("Inventario y repuestos"),
    "condition": gettext_lazy("Mantenimiento por condición"),
    "assistant": gettext_lazy("Asistente de diagnóstico"),
    "custom_roles": gettext_lazy("Roles personalizados"),
}

# Qué gana el cliente con cada módulo, en una línea (comparación de planes en Facturación, ORG-13).
MODULE_DESCRIPTIONS = {
    "assets": gettext_lazy("Ficha de cada equipo con su historial, medidores y etiqueta QR."),
    "work_orders": gettext_lazy("Crea, asigna y cierra OT desde el computador o el teléfono."),
    "preventive": gettext_lazy("Planes por fecha o por uso que generan solos las OT a tiempo."),
    "requests": gettext_lazy("Cualquiera avisa una falla desde el celular o escaneando el QR del equipo."),
    "kpis": gettext_lazy("Backlog, preventivo vs. correctivo, costos y Pareto de fallas, con exportación."),
    "api": gettext_lazy("Conecta tu ERP u otros sistemas con la API y avisos por webhook."),
    "exports": gettext_lazy("Descarga listas en Excel y fichas de activos y OT en PDF."),
    "sso": gettext_lazy("Tu equipo entra con la cuenta de la empresa, sin otra contraseña."),
    "inventory": gettext_lazy("Repuestos, bodegas, stock y aviso de reposición, ligados a las OT."),
    "condition": gettext_lazy("Avisa y crea la OT cuando una variable medida sale de rango."),
    "assistant": gettext_lazy("IA que orienta el diagnóstico con el historial y los manuales del equipo."),
    "custom_roles": gettext_lazy("Permisos a la medida de cada cargo de tu equipo."),
}


@dataclass(frozen=True)
class PlanFeature:
    """Una fila de la comparación de planes. `module` vacío = va en todos los planes."""

    key: str
    label: str
    description: str
    module: str | None


# Filas de la comparación de planes, en este orden. Las de módulo toman nombre y descripción de
# MODULE_LABELS y MODULE_DESCRIPTIONS; dos se derivan: los indicadores básicos están en el inicio
# de todos los planes (no se bloquean) y el modo sin conexión es la vista de terreno, que va con
# `work_orders` (ADR-011). El soporte no está modelado por plan: no se muestra.
_FEATURE_ORDER = [
    "assets",
    "work_orders",
    "offline",
    "preventive",
    "requests",
    "basic_kpis",
    "kpis",
    "exports",
    "inventory",
    "condition",
    "assistant",
    "api",
    "custom_roles",
    "sso",
]
_DERIVED_FEATURES = {
    "basic_kpis": (
        gettext_lazy("Indicadores básicos"),
        gettext_lazy("Disponibilidad, MTBF, MTTR y cumplimiento del preventivo en el inicio."),
        None,
    ),
    "offline": (
        gettext_lazy("Modo sin conexión en terreno"),
        gettext_lazy("Los técnicos trabajan las OT sin señal; se sincroniza al volver."),
        "work_orders",
    ),
}
_MODULE_TITLES = {
    "assets": gettext_lazy("Activos y códigos QR"),
    "requests": gettext_lazy("Solicitudes y aviso por QR"),
    "kpis": gettext_lazy("Indicadores ampliados"),
    "api": gettext_lazy("API y webhooks"),
    "assistant": gettext_lazy("Asistente de diagnóstico con IA"),
}


def plan_features() -> list[PlanFeature]:
    """Filas de la comparación de planes (el orden es el de la página)."""
    rows = []
    for key in _FEATURE_ORDER:
        if key in _DERIVED_FEATURES:
            label, description, module = _DERIVED_FEATURES[key]
        else:
            label = _MODULE_TITLES.get(key, MODULE_LABELS[key])
            description, module = MODULE_DESCRIPTIONS[key], key
        rows.append(PlanFeature(key, str(label), str(description), module))
    return rows


def plan_includes(plan, module: str | None) -> bool:
    """¿El plan incluye el módulo? Sin plan, sin módulo o con `features` vacío: sí (sin restricción)."""
    if module is None or plan is None or not plan.features:
        return True
    return module in plan.features


def modules_lost(current, target) -> list[str]:
    """Nombres de los módulos que `current` incluye y `target` no (al bajar de plan)."""
    return [
        str(label)
        for module, label in MODULE_LABELS.items()
        if plan_includes(current, module) and not plan_includes(target, module)
    ]


# Rutas web → módulo. Gana la primera que coincide (las más específicas van primero).
MODULE_ROUTES = [
    # Inventario (INV-16): los Excel piden `exports` aquí y además `inventory` en la vista.
    (re.compile(r"^/inventory/(export|movements/export|reorder/export)\.xlsx$"), "exports"),
    (re.compile(r"^/inventory/"), "inventory"),
    # Mantenimiento por condición (CBM-08): alertas y reglas (pantallas en C2).
    (re.compile(r"^/condition/"), "condition"),
    # Alta de variables desde la ficha (CBM-01); editarlas o desactivarlas sigue sin el módulo.
    (re.compile(r"^/assets/[^/]+/variables/new/$"), "condition"),
    # Asistente de diagnóstico (AI-01..05): conversaciones (pantallas en A2).
    (re.compile(r"^/assistant/"), "assistant"),
    (re.compile(r"^/(assets|work-orders|work-orders/backlog|plans/calendar)/export\.xlsx$"), "exports"),
    (re.compile(r"^/(assets|work-orders)/[^/]+/sheet\.pdf$"), "exports"),
    # Indicadores ampliados (KPI-02): la exportación pide además `kpis` en la vista.
    (re.compile(r"^/reports/kpis/export\.xlsx$"), "exports"),
    (re.compile(r"^/reports/"), "kpis"),
    (re.compile(r"^/settings/api-tokens/"), "api"),
    # Webhooks salientes (X-08, ADR-030): van con la API (Profesional y Empresa).
    (re.compile(r"^/settings/webhooks/"), "api"),
    # Inicio de sesión único (ORG-11, ADR-032): solo Empresa. El ingreso (/accounts/sso/) no lleva sesión.
    (re.compile(r"^/settings/sso/"), "sso"),
    # Roles personalizados (ORG-12, ADR-031): Profesional y Empresa.
    (re.compile(r"^/settings/roles/"), "custom_roles"),
    # Vista de terreno (ADR-011); el Service Worker y el manifest no llevan datos y quedan fuera.
    # /field/api/ responde en JSON: su módulo (`work_orders` o `requests`) lo revisa `FieldModule`.
    (re.compile(r"^/field/(?!sw\.js$|manifest\.webmanifest$|api/)"), "work_orders"),
    (re.compile(r"^/plans/"), "preventive"),
    (re.compile(r"^/assets/[^/]+/plans/"), "preventive"),
    (re.compile(r"^/(requests|report)/"), "requests"),
    (re.compile(r"^/(work-orders|failure-codes)/"), "work_orders"),
    (re.compile(r"^/(assets|asset-classes|q|manuals)/"), "assets"),
]

# Endpoints de la API v1 (basename del router) → módulo, además del módulo `api`.
API_MODULES = {
    "api-asset": "assets",
    "api-meter": "assets",
    "api-reading": "assets",
    "api-work-order": "work_orders",
    "api-request": "requests",
    "api-part": "inventory",
    "api-warehouse": "inventory",
    "api-stock": "inventory",
    "api-stock-movement": "inventory",
    # API de condición (CBM-09).
    "api-condition-rule": "condition",
    "api-condition-alert": "condition",
}


def module_for_path(path: str) -> str | None:
    for pattern, module in MODULE_ROUTES:
        if pattern.match(path):
            return module
    return None


def has_module(organization, module: str) -> bool:
    if organization is None:
        return True
    return plan_includes(organization.plan, module)


def plan_message(organization, module: str) -> str:
    plan = organization.plan.name if organization and organization.plan else ""
    return _(
        "%(module)s no está incluido en tu plan %(plan)s. "
        "Escríbenos a soporte para cambiar de plan y activarlo."
    ) % {"module": MODULE_LABELS.get(module, module), "plan": plan}


class ModuleNotIncludedError(PermissionDeniedError):
    """Un servicio pidió algo de un módulo que el plan no incluye (p. ej. `part` sin `inventory`).

    `message` es el mensaje de plan: la web lo muestra y la API responde 403 con él.
    """

    def __init__(self, organization, module: str):
        self.module = module
        self.message = plan_message(organization, module)
        super().__init__(self.message)


class PlanModuleMiddleware:
    """403 con mensaje de plan para las URL de módulos que el plan no incluye (web)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        organization = getattr(request, "organization", None)
        if organization is not None:
            module = module_for_path(request.path)
            if module and not has_module(organization, module):
                return render(
                    request,
                    "organizations/plan_module.html",
                    {"message": plan_message(organization, module)},
                    status=403,
                )
        return self.get_response(request)


class PlanIncludesModule(permissions.BasePermission):
    """API: el plan debe incluir `api` y el módulo del endpoint."""

    message = gettext_lazy("Tu plan no incluye este módulo.")

    def has_permission(self, request, view):
        organization = getattr(request.auth, "organization", None)
        if organization is None:
            return True  # sin token la autenticación ya responde 401
        for module in ("api", API_MODULES.get(getattr(view, "basename", None))):
            if module and not has_module(organization, module):
                self.message = plan_message(organization, module)
                return False
        return True
