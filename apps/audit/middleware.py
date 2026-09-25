import uuid

from .context import RequestOrigin, reset_request_origin, set_request_origin


class AuditContextMiddleware:
    """Guarda el origen de la petición (IP, navegador, ID) para `audit.services.record`."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = set_request_origin(RequestOrigin.from_request(request, request_id=uuid.uuid4().hex))
        try:
            return self.get_response(request)
        finally:
            reset_request_origin(token)
