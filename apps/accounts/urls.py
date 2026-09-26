from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("owner/employees/", views.employee_list, name="employees"),
    path("owner/employees/new/", views.employee_create, name="employee-create"),
    path("owner/employees/<int:pk>/", views.employee_edit, name="employee-edit"),
]
