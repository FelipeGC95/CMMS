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

from django.shortcuts import render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from rest_framework import permissions

MODULE_LABELS = {
    "assets": gettext_lazy("Activos"),
    "work_orders": gettext_lazy("Órdenes de trabajo"),
    "preventive": gettext_lazy("Mantenimiento preventivo"),
    "requests": gettext_lazy("Solicitudes"),
    "kpis": gettext_lazy("Indicadores"),
    "api": gettext_lazy("API"),
    "exports": gettext_lazy("Exportación a Excel y PDF"),
    "sso": gettext_lazy("Inicio de sesión único (SSO)"),
}

# Rutas web → módulo. Gana la primera que coincide (las más específicas van primero).
MODULE_ROUTES = [
    (re.compile(r"^/(assets|work-orders|work-orders/backlog|plans/calendar)/export\.xlsx$"), "exports"),
    (re.compile(r"^/(assets|work-orders)/[^/]+/sheet\.pdf$"), "exports"),
    # Indicadores ampliados (KPI-02): la exportación pide además `kpis` en la vista.
    (re.compile(r"^/reports/kpis/export\.xlsx$"), "exports"),
    (re.compile(r"^/reports/"), "kpis"),
    (re.compile(r"^/settings/api-tokens/"), "api"),
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
}


def module_for_path(path: str) -> str | None:
    for pattern, module in MODULE_ROUTES:
        if pattern.match(path):
            return module
    return None


def has_module(organization, module: str) -> bool:
    if organization is None or organization.plan is None or not organization.plan.features:
        return True
    return module in organization.plan.features


def plan_message(organization, module: str) -> str:
    plan = organization.plan.name if organization and organization.plan else ""
    return _(
        "%(module)s no está incluido en tu plan %(plan)s. "
        "Escríbenos a soporte para cambiar de plan y activarlo."
    ) % {"module": MODULE_LABELS.get(module, module), "plan": plan}


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
