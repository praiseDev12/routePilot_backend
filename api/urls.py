from django.urls import path

from . import views

urlpatterns = [
    path("health", views.health),
    path("auth/login", views.login),
    path("route", views.route),
]
