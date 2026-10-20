from django.urls import path

from . import views

urlpatterns = [
    path("settings/billing/", views.billing_view, name="billing"),
    path("settings/billing/checkout/", views.billing_checkout_view, name="billing_checkout"),
    path("settings/billing/change/", views.billing_change_view, name="billing_change"),
    path("settings/billing/cancel/", views.billing_cancel_view, name="billing_cancel"),
    path("billing/webhooks/mercadopago/", views.mercadopago_webhook_view, name="billing_webhook_mercadopago"),
]
