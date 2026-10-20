"""Ingreso por SSO (sin sesión): van bajo /accounts/, abiertas para MFARequiredMiddleware."""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.sso_login_view, name="sso_login"),
    path("callback/", views.sso_callback_view, name="sso_callback"),
]
