from django.urls import path

from .views import LoginView, LogoutView, MeView, SignupView, UserListView

urlpatterns = [
    path("signup/", SignupView.as_view(), name="api-signup"),
    path("login/", LoginView.as_view(), name="api-login"),
    path("logout/", LogoutView.as_view(), name="api-logout"),
    path("me/", MeView.as_view(), name="api-me"),
    path("users/", UserListView.as_view(), name="api-users"),
]
