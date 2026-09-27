def tenant(request):
    from apps.accounts.mfa import mfa_required

    membership = getattr(request, "membership", None)
    return {
        "organization": getattr(request, "organization", None),
        "membership": membership,
        "memberships": getattr(request, "memberships", []),
        "mfa_required": mfa_required(membership),
    }
