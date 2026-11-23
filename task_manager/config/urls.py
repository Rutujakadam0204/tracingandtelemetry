from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView
from config.metrics import metrics_view

urlpatterns = [
    path("metrics", metrics_view),
    path("admin/", admin.site.urls),
    # API
    path("api/auth/", include("accounts.urls")),
    path("api/", include("tasks.urls")),
    # HTML screens (they talk to the API with fetch)
    path("", TemplateView.as_view(template_name="tasks.html"), name="home"),
    path("login/", TemplateView.as_view(template_name="login.html"), name="login"),
    path("signup/", TemplateView.as_view(template_name="signup.html"), name="signup"),
]
