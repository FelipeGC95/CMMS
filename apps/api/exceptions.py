"""Errores de la API con forma única: {"error": {"code", "message", "details"}}, en español."""

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.utils.translation import gettext as _
from rest_framework import exceptions
from rest_framework.views import exception_handler as drf_exception_handler

from apps.accounts.permissions import PermissionDeniedError


def _messages(error: DjangoValidationError):
    if hasattr(error, "error_dict"):
        return {
            ("non_field_errors" if field == "__all__" else field): messages
            for field, messages in error.message_dict.items()
        }
    return {"non_field_errors": list(error.messages)}


def exception_handler(exc, context):
    """Errores con forma única: {"error": {"code", "message", "details"}} y textos en español."""
    if isinstance(exc, DjangoValidationError):
        exc = exceptions.ValidationError(_messages(exc))
    elif isinstance(exc, PermissionDeniedError | DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()
    elif isinstance(exc, Http404):
        exc = exceptions.NotFound(_("No encontrado."))
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, exceptions.ValidationError):
        code, message, details = "invalid", _("Revisa los datos enviados."), response.data
    else:
        detail = response.data.get("detail", "") if isinstance(response.data, dict) else response.data
        code = getattr(detail, "code", None) or getattr(exc, "default_code", "error")
        message, details = str(detail), {}
    response.data = {"error": {"code": code, "message": message, "details": details}}
    return response
