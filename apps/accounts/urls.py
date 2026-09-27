from django.urls import path

from . import views

urlpatterns = [
    path("team/", views.team_view, name="team"),
    path("team/invitations/<uuid:pk>/revoke/", views.invitation_revoke_view, name="invitation_revoke"),
    path("team/members/<uuid:pk>/", views.member_edit_view, name="member_edit"),
    path("team/members/<uuid:pk>/active/", views.member_active_view, name="member_active"),
    path("invitations/<str:token>/", views.invitation_accept_view, name="invitation_accept"),
    path("signup/", views.signup_view, name="signup"),
    path("signup/sent/", views.signup_sent_view, name="signup_sent"),
    path("signup/confirm/<str:token>/", views.signup_confirm_view, name="signup_confirm"),
]
