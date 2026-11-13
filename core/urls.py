from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("login/", views.login_page, name="login"),
    path("auth/session/", views.session_login, name="session_login"),
    path("logout/", views.logout_view, name="logout"),
    # citizen
    path("my/", views.my_reports, name="my_reports"),
    path("report/new/", views.report_new, name="report_new"),
    path("report/<str:rid>/", views.report_detail, name="report_detail"),
    # admin
    path("panel/", views.admin_panel, name="admin_panel"),
    path("panel/report/<str:rid>/update/", views.admin_update, name="admin_update"),
]
