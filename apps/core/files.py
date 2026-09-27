"""Entrega de archivos privados (`org/<organization_id>/…`, ADR-014, NF-06).

La vista que llama ya verificó organización y permisos. Con almacenamiento local
el archivo se transmite desde el servidor; en R2 se redirige a una URL firmada
que vence en pocos minutos (el bucket nunca es público).
"""

from django.http import FileResponse, HttpResponseRedirect

from config.infra import SIGNED_URL_SECONDS


def uses_signed_urls(storage) -> bool:
    return bool(getattr(storage, "querystring_auth", False))


def private_file_response(fieldfile, *, content_type: str, filename: str | None = None):
    storage = fieldfile.storage
    if uses_signed_urls(storage):
        parameters = {"ResponseContentType": content_type}
        if filename:
            # Mismo comportamiento que FileResponse: inline, con el nombre original.
            ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "file"
            parameters["ResponseContentDisposition"] = f'inline; filename="{ascii_name}"'
        url = storage.url(fieldfile.name, parameters=parameters, expire=SIGNED_URL_SECONDS)
        return HttpResponseRedirect(url)
    return FileResponse(fieldfile.open("rb"), content_type=content_type, filename=filename)
