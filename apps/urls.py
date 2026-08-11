from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('', views.dashboard_view, name='dashboard'),

    # Module 1: Facility Management
    path('facilities/', views.facilities_view, name='facilities'),
    path('facilities/create/', views.facility_create_view, name='facility_create'),
    path('facilities/<int:pk>/', views.facility_detail_view, name='facility_detail'),
    path('facilities/<int:pk>/edit/', views.facility_edit_view, name='facility_edit'),

    # Module 2: Booking
    path('bookings/', views.bookings_view, name='bookings'),
    path('bookings/create/', views.booking_create_view, name='booking_create'),
    path('bookings/<int:pk>/', views.booking_detail_view, name='booking_detail'),
    path('bookings/<int:pk>/action/', views.booking_approve_view, name='booking_action'),
    path('bookings/<int:pk>/cancel/', views.booking_cancel_view, name='booking_cancel'),
    path('bookings/check-conflict/', views.booking_check_conflict, name='booking_check_conflict'),

    # Module 3: User Management
    path('users/', views.user_list_view, name='user_list'),
    path('users/create/', views.user_create_view, name='user_create'),
    path('users/<int:pk>/edit/', views.user_edit_view, name='user_edit'),
    path('users/<int:pk>/toggle-lock/', views.user_toggle_lock_view, name='user_toggle_lock'),
    path('users/<int:pk>/activity/', views.user_activity_view, name='user_activity'),

    # Module 4: Notifications
    path('notifications/', views.notifications_view, name='notifications'),
    path('notifications/<int:pk>/read/', views.notification_mark_read, name='notification_read'),
    path('notifications/<int:pk>/delete/', views.notification_delete, name='notification_delete'),
    path('announcements/', views.announcement_list_view, name='announcement_list'),
    path('announcements/create/', views.announcement_create_view, name='announcement_create'),
    path('notifications/templates/', views.template_list_view, name='notification_templates'),
    path('notifications/templates/<int:pk>/edit/', views.template_edit_view, name='template_edit'),

    # Module 5: Reports
    path('reports/', views.reports_view, name='reports'),
    path('reports/export/csv/', views.reports_export_csv, name='reports_export_csv'),
]