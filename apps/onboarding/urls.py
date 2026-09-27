from django.urls import path

from . import views

urlpatterns = [
    path("onboarding/", views.onboarding_view, name="onboarding"),
    path("onboarding/dismiss/", views.dismiss_view, name="onboarding_dismiss"),
    path("onboarding/<slug:step>/", views.step_view, name="onboarding_step"),
    path("onboarding/<slug:step>/skip/", views.skip_view, name="onboarding_skip"),
]
