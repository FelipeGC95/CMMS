"""Rutas web del asistente de diagnóstico (incluidas en config/urls.py bajo `assistant/`)."""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.conversation_list_view, name="assistant_list"),
    path("new/", views.conversation_start_view, name="assistant_start"),
    path("<uuid:pk>/", views.conversation_detail_view, name="assistant_conversation"),
    path("<uuid:pk>/thread/", views.conversation_thread_view, name="assistant_thread"),
    path("<uuid:pk>/ask/", views.conversation_ask_view, name="assistant_ask"),
    path("<uuid:pk>/summary/", views.conversation_summary_view, name="assistant_summary"),
    path("<uuid:pk>/helpful/", views.conversation_helpful_view, name="assistant_helpful"),
    path("<uuid:pk>/close/", views.conversation_close_view, name="assistant_close"),
]
