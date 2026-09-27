"""Interruptores de infraestructura por entorno (docs/07_operacion.md)."""

import json
import logging

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db.models.fields.files import FieldFile
from django.http import FileResponse
from django.urls import reverse

from apps.core.files import private_file_response
from apps.core.logs import JsonFormatter, RequestIdFilter, RequestIdMiddleware
from config import infra

R2_ENV = {
    "R2_BUCKET": "cmms-prod",
    "R2_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
    "R2_ACCESS_KEY_ID": "key",
    "R2_SECRET_ACCESS_KEY": "secret",
}


def test_storage_is_local_without_r2_vars():
    assert infra.default_storage_config({}) == {"BACKEND": "django.core.files.storage.FileSystemStorage"}


def test_storage_stays_local_when_an_r2_var_is_missing():
    partial = {k: v for k, v in R2_ENV.items() if k != "R2_SECRET_ACCESS_KEY"}
    assert infra.default_storage_config(partial)["BACKEND"].endswith("FileSystemStorage")


def test_storage_switches_to_private_r2_with_all_vars():
    config = infra.default_storage_config(R2_ENV)
    assert config["BACKEND"] == "storages.backends.s3.S3Storage"
    options = config["OPTIONS"]
    assert options["bucket_name"] == "cmms-prod"
    assert options["endpoint_url"] == R2_ENV["R2_ENDPOINT_URL"]
    assert options["querystring_auth"] is True
    assert options["querystring_expire"] == 300
    assert options["default_acl"] is None
    assert options["file_overwrite"] is False


def test_r2_storage_signs_short_lived_urls():
    from storages.backends.s3 import S3Storage

    storage = S3Storage(**infra.default_storage_config(R2_ENV)["OPTIONS"])
    url = storage.url("org/abc/file.pdf")
    assert url.startswith("https://account.r2.cloudflarestorage.com/cmms-prod/org/abc/file.pdf?")
    assert "X-Amz-Expires=300" in url
    assert "X-Amz-Signature=" in url


def test_sentry_is_off_without_dsn_and_never_sends_pii():
    assert infra.sentry_options({}) is None
    options = infra.sentry_options(
        {"SENTRY_DSN": "https://k@o0.ingest.sentry.io/1", "SENTRY_ENVIRONMENT": "staging"}
    )
    assert options["send_default_pii"] is False
    assert options["environment"] == "staging"
    assert options["traces_sample_rate"] == 0


def test_logging_is_plain_by_default_and_json_on_demand():
    assert "formatters" not in infra.logging_config({})
    config = infra.logging_config({"LOG_FORMAT": "json"})
    assert config["handlers"]["console"]["formatter"] == "json"
    assert config["handlers"]["console"]["filters"] == ["request_id"]


def test_json_log_line_carries_request_id(rf):
    captured = {}

    def view(request):
        record = logging.LogRecord("cmms", logging.INFO, __file__, 1, "hola %s", ("mundo",), None)
        RequestIdFilter().filter(record)
        captured["line"] = JsonFormatter().format(record)
        from django.http import HttpResponse

        return HttpResponse("ok")

    response = RequestIdMiddleware(view)(rf.get("/", HTTP_X_REQUEST_ID="abc123"))
    payload = json.loads(captured["line"])
    assert payload["message"] == "hola mundo"
    assert payload["request_id"] == "abc123"
    assert response["X-Request-ID"] == "abc123"


def test_request_id_is_generated_when_missing(rf):
    from django.http import HttpResponse

    response = RequestIdMiddleware(lambda request: HttpResponse("ok"))(rf.get("/"))
    assert len(response["X-Request-ID"]) == 32


def test_healthz_and_readyz(client, db):
    assert client.get(reverse("healthz")).content == b"ok"
    response = client.get(reverse("readyz"))
    assert response.status_code == 200
    assert response.content == b"ok"


def test_readyz_reports_503_when_database_fails(client, db, monkeypatch):
    from apps.core import views

    class BrokenConnection:
        def cursor(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(views, "connection", BrokenConnection())
    assert client.get(reverse("readyz")).status_code == 503


class _Holder:
    """Instancia mínima para construir un FieldFile fuera de un modelo."""

    class _Field:
        storage = None

    def __init__(self, storage):
        self.field = self._Field()
        self.field.storage = storage


def _fieldfile(storage, name):
    holder = _Holder(storage)
    return FieldFile(holder, holder.field, name)


def test_private_file_is_streamed_with_local_storage(tmp_path, db):
    storage = FileSystemStorage(location=tmp_path)
    name = storage.save("org/abc/manual.pdf", ContentFile(b"%PDF"))
    response = private_file_response(
        _fieldfile(storage, name), content_type="application/pdf", filename="manual.pdf"
    )
    assert isinstance(response, FileResponse)
    assert b"".join(response.streaming_content) == b"%PDF"
    response.close()


def test_private_file_redirects_to_signed_url_on_r2():
    from storages.backends.s3 import S3Storage

    storage = S3Storage(**infra.default_storage_config(R2_ENV)["OPTIONS"])
    response = private_file_response(
        _fieldfile(storage, "org/abc/manual.pdf"), content_type="application/pdf", filename="Manual ñ.pdf"
    )
    assert response.status_code == 302
    location = response["Location"]
    assert "/cmms-prod/org/abc/manual.pdf?" in location
    assert "X-Amz-Expires=300" in location
    assert "response-content-type=application%2Fpdf" in location
