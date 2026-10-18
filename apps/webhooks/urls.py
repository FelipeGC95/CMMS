from django.urls import path

from . import views

urlpatterns = [
    path("", views.webhook_list_view, name="webhook_list"),
    path("<uuid:pk>/", views.webhook_detail_view, name="webhook_detail"),
    path("<uuid:pk>/test/", views.webhook_test_view, name="webhook_test"),
]
