def tenant(request):
    return {
        "organization": getattr(request, "organization", None),
        "membership": getattr(request, "membership", None),
        "memberships": getattr(request, "memberships", []),
    }
