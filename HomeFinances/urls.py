from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from finance import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', RedirectView.as_view(pattern_name='main_page', permanent=False)),
    path('register_page/', views.registration_view, name='register_page'),
    path('enter_site/', views.enter_site, name='enter_site'),
    path('confirmation/', views.confirmation, name='confirmation'),
    path('resend_code/', views.resend_code, name='resend_code'),
    path('main_page/', views.main_page, name='main_page'),
    path('logout/', views.logout_view, name='logout'),
    path('expenses/', views.expenses, name='expenses'),
    path('income/', views.income, name='income'),
    path('report/', views.report, name='report'),
    path('goals/', views.goals, name='goals'),
    path('budget/', views.budget, name='budget'),
    path('profile/', views.profile, name='profile'),
]