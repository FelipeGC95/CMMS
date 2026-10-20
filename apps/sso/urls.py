from django.urls import path

from . import views

urlpatterns = [
    path("", views.sso_settings_view, name="sso_settings"),
    path("test/", views.sso_test_view, name="sso_test"),
    path("domains/", views.sso_domain_add_view, name="sso_domain_add"),
    path("domains/<uuid:pk>/verify/", views.sso_domain_verify_view, name="sso_domain_verify"),
    path("domains/<uuid:pk>/remove/", views.sso_domain_remove_view, name="sso_domain_remove"),
]
