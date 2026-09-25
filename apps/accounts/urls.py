from django.urls import path

from . import views

urlpatterns = [
    path("team/", views.team_view, name="team"),
    path("team/invitations/<uuid:pk>/revoke/", views.invitation_revoke_view, name="invitation_revoke"),
    path("team/members/<uuid:pk>/", views.member_edit_view, name="member_edit"),
    path("team/members/<uuid:pk>/active/", views.member_active_view, name="member_active"),
    path("invitations/<str:token>/", views.invitation_accept_view, name="invitation_accept"),
]
