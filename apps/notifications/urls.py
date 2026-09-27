from django.urls import path

from . import views

urlpatterns = [
    path("notifications/", views.notification_list_view, name="notification_list"),
    path("notifications/badge/", views.notification_badge_view, name="notification_badge"),
    path("notifications/read-all/", views.notification_read_all_view, name="notification_read_all"),
    path("notifications/<uuid:pk>/open/", views.notification_open_view, name="notification_open"),
    path("account/notifications/", views.notification_preferences_view, name="notification_preferences"),
]
