from django.urls import path

from . import views

urlpatterns = [
    path("locations/", views.location_list_view, name="location_list"),
    path("locations/<uuid:pk>/", views.location_edit_view, name="location_edit"),
    path("locations/<uuid:pk>/delete/", views.location_delete_view, name="location_delete"),
]
