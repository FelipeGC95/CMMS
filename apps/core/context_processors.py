def tenant(request):
    from apps.accounts.mfa import mfa_required

    membership = getattr(request, "membership", None)
    status = getattr(request, "billing_status", None)
    return {
        "organization": getattr(request, "organization", None),
        "membership": membership,
        "memberships": getattr(request, "memberships", []),
        "mfa_required": mfa_required(membership),
        # Estado de facturación (ORG-13) que deja ReadOnlyMiddleware: avisos y solo lectura.
        "billing_status": status,
        # Solo lectura (ORG-13): las plantillas ocultan las acciones que no pasan por permisos.
        "read_only": status is not None and status.read_only,
    }
