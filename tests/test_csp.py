"""ADR-023: CSP estricta en todas las respuestas y plantillas sin JS ni estilos en línea."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from config.infra import content_security_policy
from tests.conftest import login

TEMPLATES = Path(settings.BASE_DIR) / "templates"
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I)
INLINE_STYLE = re.compile(r"<style\b|\sstyle\s*=", re.I)
EVENT_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)
TEMPLATE_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def policy(response):
    return dict(
        (part.strip().split(" ", 1) + [""])[:2] for part in response["Content-Security-Policy"].split(";")
    )


@pytest.mark.parametrize("url_name", ["account_login", "signup"])
def test_public_pages_send_strict_csp(client, db, url_name):
    directives = policy(client.get(reverse(url_name)))
    assert directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["form-action"] == "'self'"
    assert "unsafe" not in " ".join(directives.values())
    assert "data:" in directives["img-src"] and "blob:" in directives["img-src"]


def test_signed_in_pages_send_csp(client, tenant_a):
    login(client, tenant_a.admin)
    for url in (reverse("dashboard"), reverse("work_order_list"), reverse("organization_settings")):
        assert "script-src 'self'" in client.get(url)["Content-Security-Policy"]


def test_r2_host_is_allowed_for_images():
    env = {
        "R2_BUCKET": "b",
        "R2_ENDPOINT_URL": "https://abc123.r2.cloudflarestorage.com",
        "R2_ACCESS_KEY_ID": "k",
        "R2_SECRET_ACCESS_KEY": "s",
        "CSP_EXTRA_IMG_SRC": "https://cdn.example.com",
    }
    img_src = dict(p.strip().split(" ", 1) for p in content_security_policy(env).split(";"))["img-src"]
    assert "https://abc123.r2.cloudflarestorage.com" in img_src
    assert "https://*.abc123.r2.cloudflarestorage.com" in img_src
    assert "https://cdn.example.com" in img_src
    assert "r2" not in content_security_policy({})


def test_swagger_ui_has_no_inline_code(client, db):
    html = client.get(reverse("api_docs")).content.decode()
    assert not INLINE_SCRIPT.search(html) and not INLINE_STYLE.search(html)
    assert "swagger.css" in html


def test_base_disables_htmx_inline_indicator_styles(client, db):
    assert '"includeIndicatorStyles": false' in (TEMPLATES / "base.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", sorted(TEMPLATES.rglob("*.html")), ids=lambda p: p.name)
def test_templates_have_no_inline_js_or_styles(path):
    source = TEMPLATE_COMMENT.sub("", path.read_text(encoding="utf-8"))
    rel = path.relative_to(TEMPLATES)
    assert not INLINE_SCRIPT.search(source), f"{rel}: <script> en línea"
    assert not INLINE_STYLE.search(source), f"{rel}: estilo en línea"
    assert not EVENT_HANDLER.search(source), f"{rel}: manejador de eventos en línea"
