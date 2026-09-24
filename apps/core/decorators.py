from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from apps.accounts.permissions import has_perm


def membership_required(view=None, *, perm=None):
    """Exige sesión, organización activa y (opcional) un permiso del rol."""

    def decorator(func):
        @login_required
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if request.membership is None:
                return render(request, "core/no_organization.html", status=403)
            if perm is not None and not has_perm(request.membership, perm):
                raise PermissionDenied
            return func(request, *args, **kwargs)

        return wrapper

    return decorator(view) if view else decorator
