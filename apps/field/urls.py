from django.urls import path

from . import views

urlpatterns = [
    path("field/", views.shell_view, name="field_shell"),
    path("field/sw.js", views.sw_view, name="field_sw"),
    path("field/manifest.webmanifest", views.manifest_view, name="field_manifest"),
    path("field/demo.json", views.demo_data_view, name="field_demo"),
]
