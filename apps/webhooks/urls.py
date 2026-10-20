from django.urls import path

from . import views

urlpatterns = [
    path("", views.webhook_list_view, name="webhook_list"),
    path("<uuid:pk>/", views.webhook_detail_view, name="webhook_detail"),
    path("<uuid:pk>/test/", views.webhook_test_view, name="webhook_test"),
    path("<uuid:pk>/secret/", views.webhook_secret_view, name="webhook_secret"),
    path("<uuid:pk>/deliveries/<uuid:delivery_pk>/", views.webhook_delivery_view, name="webhook_delivery"),
    path(
        "<uuid:pk>/deliveries/<uuid:delivery_pk>/redeliver/",
        views.webhook_redeliver_view,
        name="webhook_redeliver",
    ),
]
