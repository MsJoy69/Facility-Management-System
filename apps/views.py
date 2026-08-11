from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from functools import wraps
import datetime
import json
import csv
from .models import User, Facility, Booking, Notification, Announcement, NotificationTemplate, ActivityLog


# ── Helpers ───────────────────────────────────────────────────

def log_activity(user, action, description='', request=None):
    ip = request.META.get('REMOTE_ADDR') if request else None
    ActivityLog.objects.create(user=user, action=action, description=description, ip_address=ip)


def send_notification(recipient, notif_type, title, message, booking=None, sent_by=None):
    """Central helper — uses template if available, falls back to provided message."""
    try:
        tmpl = NotificationTemplate.objects.get(notif_type=notif_type, is_active=True)
        ctx = {
            'user': recipient.get_full_name() or recipient.username,
            'facility': booking.facility.name if booking else '',
            'date': str(booking.date) if booking else '',
            'start_time': booking.start_time.strftime('%I:%M %p') if booking else '',
            'end_time': booking.end_time.strftime('%I:%M %p') if booking else '',
            'status': booking.get_status_display() if booking else '',
        }
        message = tmpl.render(ctx)
        title = tmpl.subject.format(**ctx)
    except NotificationTemplate.DoesNotExist:
        pass

    return Notification.objects.create(
        recipient=recipient, notif_type=notif_type,
        title=title, message=message,
        booking=booking, sent_by=sent_by,
    )


# ── Decorators ────────────────────────────────────────────────

def facility_management_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.can_manage_facilities():
            messages.error(request, 'You do not have permission to manage facilities.')
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper


def booking_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.can_book():
            messages.error(request, 'You do not have permission to make bookings.')
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper


def superuser_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.can_manage_users():
            messages.error(request, 'Only Superusers can access User Management.')
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper


def reports_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.can_view_reports():
            messages.error(request, 'Only Facility Managers can view reports.')
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper


def announcements_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.can_send_announcements():
            messages.error(request, 'Only Facility Managers can send announcements.')
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper


# ── Auth ──────────────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        try:
            user_obj = User.objects.get(username=username)
            if user_obj.is_locked:
                messages.error(request, 'This account is locked. Contact the administrator.')
                return render(request, 'login.html')
        except User.DoesNotExist:
            pass
        user = authenticate(request, username=username, password=password)
        if user is not None:
            user.failed_login_attempts = 0
            user.last_active = timezone.now()
            user.save(update_fields=['failed_login_attempts', 'last_active'])
            login(request, user)
            log_activity(user, 'login', 'User logged in', request)
            return redirect('dashboard')
        else:
            try:
                user_obj = User.objects.get(username=username)
                user_obj.failed_login_attempts += 1
                if user_obj.failed_login_attempts >= 5:
                    user_obj.is_locked = True
                    messages.error(request, 'Account locked after 5 failed attempts.')
                else:
                    messages.error(request, f'Invalid credentials. {5 - user_obj.failed_login_attempts} attempt(s) remaining.')
                user_obj.save(update_fields=['failed_login_attempts', 'is_locked'])
            except User.DoesNotExist:
                messages.error(request, 'Invalid username or password.')
    return render(request, 'login.html')


def logout_view(request):
    if request.user.is_authenticated:
        log_activity(request.user, 'logout', 'User logged out', request)
    logout(request)
    return redirect('login')


# ── Dashboard ─────────────────────────────────────────────────

@login_required(login_url='login')
def dashboard_view(request):
    request.user.last_active = timezone.now()
    request.user.save(update_fields=['last_active'])
    today = datetime.date.today()

    # Escalation check — send alerts for pending > 24h
    if request.user.can_manage_facilities():
        yesterday = timezone.now() - datetime.timedelta(hours=24)
        old_pending = Booking.objects.filter(status='pending', created_at__lte=yesterday)
        for b in old_pending:
            already_alerted = Notification.objects.filter(
                booking=b, notif_type='escalation',
                recipient=request.user
            ).exists()
            if not already_alerted:
                Notification.objects.create(
                    recipient=request.user, notif_type='escalation',
                    title=f'Pending Approval: {b.facility.name}',
                    message=f'Booking #{b.pk} by {b.booked_by.get_full_name() or b.booked_by.username} for {b.facility.name} on {b.date} has been pending for over 24 hours.',
                    booking=b,
                )

    context = {
        'total_facilities': Facility.objects.count(),
        'active_facilities': Facility.objects.filter(status='active').count(),
        'bookings_today': Booking.objects.filter(date=today).count(),
        'pending_bookings': Booking.objects.filter(status='pending').count(),
        'recent_bookings': Booking.objects.select_related('facility', 'booked_by').all()[:5],
        'unread_notifications': request.user.notifications.filter(is_read=False).count(),
    }
    return render(request, 'dashboard.html', context)


# ── Module 1: Facility Management ─────────────────────────────

@login_required(login_url='login')
def facilities_view(request):
    floor_filter = request.GET.get('floor', '')
    status_filter = request.GET.get('status', '')
    type_filter = request.GET.get('type', '')
    facilities = Facility.objects.all()
    if floor_filter:
        facilities = facilities.filter(floor=floor_filter)
    if status_filter:
        facilities = facilities.filter(status=status_filter)
    if type_filter:
        facilities = facilities.filter(facility_type=type_filter)
    facilities = facilities.order_by('floor', 'name')
    grouped = {}
    for f in facilities:
        k = f.floor or 'Other'
        grouped.setdefault(k, []).append(f)
    return render(request, 'facilities.html', {
        'grouped_facilities': grouped, 'floor_filter': floor_filter,
        'status_filter': status_filter, 'type_filter': type_filter,
        'floor_choices': Facility.FLOOR_CHOICES, 'total': facilities.count(),
    })


@login_required(login_url='login')
def facility_detail_view(request, pk):
    facility = get_object_or_404(Facility, pk=pk)
    return render(request, 'facility_detail.html', {'facility': facility, 'recent_bookings': facility.bookings.select_related('booked_by').order_by('-created_at')[:5]})


@login_required(login_url='login')
@facility_management_required
def facility_create_view(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        location = request.POST.get('location', '').strip()
        if name and location:
            f = Facility(
                name=name, location=location,
                building=request.POST.get('building', 'CAS Building').strip(),
                floor=request.POST.get('floor', ''),
                facility_type=request.POST.get('facility_type', 'classroom'),
                capacity=request.POST.get('capacity', 0),
                description=request.POST.get('description', '').strip(),
                tags=request.POST.get('tags', '').strip(),
                custodian=request.POST.get('custodian', '').strip(),
                availability_start=request.POST.get('availability_start') or None,
                availability_end=request.POST.get('availability_end') or None,
                is_restricted=request.POST.get('is_restricted') == 'on',
                allowed_roles=request.POST.get('allowed_roles', '').strip(),
                created_by=request.user,
            )
            if 'image' in request.FILES: f.image = request.FILES['image']
            if 'floor_plan' in request.FILES: f.floor_plan = request.FILES['floor_plan']
            f.save()
            log_activity(request.user, 'create_facility', f'Created: {name}', request)
            messages.success(request, f'Facility "{name}" created.')
            return redirect('facilities')
        messages.error(request, 'Name and location are required.')
    return render(request, 'facility_form.html', {'action': 'Create', 'floor_choices': Facility.FLOOR_CHOICES, 'type_choices': Facility.TYPE_CHOICES, 'status_choices': Facility.STATUS_CHOICES})


@login_required(login_url='login')
@facility_management_required
def facility_edit_view(request, pk):
    facility = get_object_or_404(Facility, pk=pk)
    if request.method == 'POST':
        for field in ['name', 'location', 'building', 'floor', 'facility_type', 'capacity', 'description', 'tags', 'custodian', 'status']:
            setattr(facility, field, request.POST.get(field, getattr(facility, field)))
        facility.availability_start = request.POST.get('availability_start') or None
        facility.availability_end = request.POST.get('availability_end') or None
        facility.is_restricted = request.POST.get('is_restricted') == 'on'
        facility.allowed_roles = request.POST.get('allowed_roles', '').strip()
        if 'image' in request.FILES: facility.image = request.FILES['image']
        if 'floor_plan' in request.FILES: facility.floor_plan = request.FILES['floor_plan']
        if request.POST.get('clear_image'): facility.image = None
        if request.POST.get('clear_floor_plan'): facility.floor_plan = None
        facility.save()
        log_activity(request.user, 'edit_facility', f'Edited: {facility.name}', request)
        messages.success(request, f'Facility "{facility.name}" updated.')
        return redirect('facility_detail', pk=facility.pk)
    return render(request, 'facility_form.html', {'facility': facility, 'action': 'Edit', 'floor_choices': Facility.FLOOR_CHOICES, 'type_choices': Facility.TYPE_CHOICES, 'status_choices': Facility.STATUS_CHOICES})


# ── Module 2: Booking ─────────────────────────────────────────

def check_booking_conflict(facility, date, start_time, end_time, exclude_pk=None):
    conflicts = Booking.objects.filter(facility=facility, date=date, status__in=[Booking.PENDING, Booking.APPROVED]).exclude(pk=exclude_pk or 0)
    return [b for b in conflicts if b.start_time < end_time and b.end_time > start_time]


@login_required(login_url='login')
def bookings_view(request):
    view_mode = request.GET.get('view', 'list')
    status_filter = request.GET.get('status', '')
    facility_filter = request.GET.get('facility', '')
    bookings = Booking.objects.select_related('facility', 'booked_by').all() if request.user.can_manage_facilities() else Booking.objects.filter(booked_by=request.user).select_related('facility')
    if status_filter: bookings = bookings.filter(status=status_filter)
    if facility_filter: bookings = bookings.filter(facility_id=facility_filter)
    today = datetime.date.today()
    calendar_bookings = [{'id': b.pk, 'title': f'{b.facility.name} — {b.booked_by.get_full_name() or b.booked_by.username}', 'start': f'{b.date}T{b.start_time}', 'end': f'{b.date}T{b.end_time}', 'color': '#1a6b3a' if b.status == 'approved' else '#b7770d', 'status': b.status, 'facility': b.facility.name, 'booked_by': b.booked_by.get_full_name() or b.booked_by.username, 'purpose': b.purpose} for b in Booking.objects.select_related('facility', 'booked_by').filter(status__in=['pending', 'approved'])]
    stats = {'total': Booking.objects.count() if request.user.can_manage_facilities() else Booking.objects.filter(booked_by=request.user).count(), 'pending': Booking.objects.filter(status='pending').count() if request.user.can_manage_facilities() else Booking.objects.filter(booked_by=request.user, status='pending').count(), 'approved': Booking.objects.filter(status='approved').count() if request.user.can_manage_facilities() else Booking.objects.filter(booked_by=request.user, status='approved').count(), 'today': Booking.objects.filter(date=today).count()}
    return render(request, 'bookings.html', {'bookings': bookings, 'calendar_bookings_json': json.dumps(calendar_bookings), 'view_mode': view_mode, 'status_filter': status_filter, 'facility_filter': facility_filter, 'facilities': Facility.objects.filter(status='active'), 'stats': stats, 'today': today})


@login_required(login_url='login')
def booking_check_conflict(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST required'}, status=405)
    try:
        data = json.loads(request.body)
        facility = Facility.objects.get(pk=data.get('facility_id'))
        date = datetime.date.fromisoformat(data.get('date'))
        start_time = datetime.time.fromisoformat(data.get('start_time'))
        end_time = datetime.time.fromisoformat(data.get('end_time'))
        if start_time >= end_time: return JsonResponse({'conflict': False, 'error': 'End time must be after start time.'})
        conflicts = check_booking_conflict(facility, date, start_time, end_time, data.get('exclude_pk'))
        if conflicts:
            suggestions = []
            for offset in [1, 2, 3]:
                alt_start = (datetime.datetime.combine(date, end_time) + datetime.timedelta(hours=offset)).time()
                duration = datetime.datetime.combine(date, end_time) - datetime.datetime.combine(date, start_time)
                alt_end = (datetime.datetime.combine(date, alt_start) + duration).time()
                if not check_booking_conflict(facility, date, alt_start, alt_end, data.get('exclude_pk')) and alt_end.hour < 21:
                    suggestions.append({'start': str(alt_start)[:5], 'end': str(alt_end)[:5]})
                    if len(suggestions) >= 2: break
            return JsonResponse({'conflict': True, 'conflicts': [{'booked_by': c.booked_by.get_full_name() or c.booked_by.username, 'start_time': str(c.start_time), 'end_time': str(c.end_time), 'status': c.get_status_display()} for c in conflicts], 'suggestions': suggestions})
        return JsonResponse({'conflict': False})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required(login_url='login')
@booking_required
def booking_create_view(request):
    facilities = Facility.objects.filter(status='active')
    preselect_facility = request.GET.get('facility', '')
    if request.method == 'POST':
        facility_id = request.POST.get('facility')
        date_str = request.POST.get('date')
        start_str = request.POST.get('start_time')
        end_str = request.POST.get('end_time')
        purpose = request.POST.get('purpose', '').strip()
        attendees = request.POST.get('number_of_attendees', 1)
        force = request.POST.get('force_submit') == '1'
        if facility_id and date_str and start_str and end_str:
            facility = get_object_or_404(Facility, pk=facility_id)
            date = datetime.date.fromisoformat(date_str)
            start_time = datetime.time.fromisoformat(start_str)
            end_time = datetime.time.fromisoformat(end_str)
            if start_time >= end_time: messages.error(request, 'End time must be after start time.')
            elif date < datetime.date.today(): messages.error(request, 'Cannot book a past date.')
            else:
                conflicts = check_booking_conflict(facility, date, start_time, end_time)
                if conflicts and not force:
                    return render(request, 'booking_form.html', {'facilities': facilities, 'has_conflict': True, 'conflict_info': conflicts, 'form_data': request.POST, 'preselect': facility_id})
                booking = Booking.objects.create(facility=facility, booked_by=request.user, date=date, start_time=start_time, end_time=end_time, purpose=purpose, number_of_attendees=attendees)
                send_notification(request.user, 'confirmation', f'Booking Submitted: {facility.name}', f'Your booking for {facility.name} on {date} is pending approval.', booking=booking)
                log_activity(request.user, 'book', f'Booked {facility.name} on {date}', request)
                messages.success(request, f'Booking #{booking.pk} submitted!')
                return redirect('bookings')
        else: messages.error(request, 'All fields are required.')
    return render(request, 'booking_form.html', {'facilities': facilities, 'preselect': preselect_facility, 'today': datetime.date.today().isoformat()})


@login_required(login_url='login')
def booking_detail_view(request, pk):
    booking = get_object_or_404(Booking, pk=pk) if request.user.can_manage_facilities() else get_object_or_404(Booking, pk=pk, booked_by=request.user)
    return render(request, 'booking_detail.html', {'booking': booking})


@login_required(login_url='login')
@facility_management_required
def booking_approve_view(request, pk):
    booking = get_object_or_404(Booking, pk=pk)
    action = request.POST.get('action')
    if action == 'approve':
        booking.status = Booking.APPROVED
        booking.approved_by = request.user
        booking.save()
        send_notification(booking.booked_by, 'approval', f'Booking Approved: {booking.facility.name}', f'Your booking for {booking.facility.name} on {booking.date} has been approved.', booking=booking, sent_by=request.user)
        log_activity(request.user, 'approve', f'Approved booking #{pk}', request)
        messages.success(request, f'Booking #{pk} approved.')
    elif action == 'reject':
        booking.status = Booking.REJECTED
        booking.save()
        send_notification(booking.booked_by, 'rejection', f'Booking Rejected: {booking.facility.name}', f'Your booking for {booking.facility.name} on {booking.date} was rejected.', booking=booking, sent_by=request.user)
        log_activity(request.user, 'reject', f'Rejected booking #{pk}', request)
        messages.warning(request, f'Booking #{pk} rejected.')
    return redirect('bookings')


@login_required(login_url='login')
def booking_cancel_view(request, pk):
    booking = get_object_or_404(Booking, pk=pk, booked_by=request.user)
    if booking.status == Booking.PENDING:
        booking.status = Booking.CANCELLED
        booking.save()
        send_notification(request.user, 'cancellation', f'Booking Cancelled: {booking.facility.name}', f'Your booking for {booking.facility.name} on {booking.date} has been cancelled.', booking=booking)
        log_activity(request.user, 'cancel', f'Cancelled booking #{pk}', request)
        messages.success(request, 'Booking cancelled.')
    else: messages.error(request, 'Only pending bookings can be cancelled.')
    return redirect('bookings')


# ── Module 3: User Management ─────────────────────────────────

@login_required(login_url='login')
@superuser_required
def user_list_view(request):
    from django.db.models import Q as DQ
    role_filter = request.GET.get('role', '')
    dept_filter = request.GET.get('dept', '')
    search = request.GET.get('search', '').strip()
    users = User.objects.all().order_by('role', 'last_name', 'first_name')
    if role_filter: users = users.filter(role=role_filter)
    if dept_filter: users = users.filter(department=dept_filter)
    if search: users = users.filter(DQ(username__icontains=search)|DQ(first_name__icontains=search)|DQ(last_name__icontains=search)|DQ(email__icontains=search))
    stats = {'total': User.objects.count(), 'active': User.objects.filter(is_active=True, is_locked=False).count(), 'locked': User.objects.filter(is_locked=True).count(), 'by_role': {r: User.objects.filter(role=r).count() for r, _ in User.ROLE_CHOICES}}
    return render(request, 'users.html', {'users': users, 'role_filter': role_filter, 'dept_filter': dept_filter, 'search': search, 'stats': stats, 'role_choices': User.ROLE_CHOICES, 'dept_choices': User.DEPARTMENT_CHOICES})


@login_required(login_url='login')
@superuser_required
def user_create_view(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')
        if not username or not password: messages.error(request, 'Username and password are required.')
        elif password != password_confirm: messages.error(request, 'Passwords do not match.')
        elif len(password) < 8: messages.error(request, 'Password must be at least 8 characters.')
        elif User.objects.filter(username=username).exists(): messages.error(request, 'Username already exists.')
        else:
            role = request.POST.get('role', User.STANDARD_USER)
            user = User.objects.create_user(username=username, first_name=request.POST.get('first_name', '').strip(), last_name=request.POST.get('last_name', '').strip(), email=request.POST.get('email', '').strip(), password=password, role=role, department=request.POST.get('department', ''), phone=request.POST.get('phone', '').strip(), profile_notes=request.POST.get('profile_notes', '').strip())
            log_activity(request.user, 'create_user', f'Created: {username} ({role})', request)
            send_notification(user, 'announcement', 'Welcome to OLFU FMS!', f'Your account has been created. Your role is: {user.get_role_display()}.', sent_by=request.user)
            messages.success(request, f'User "{username}" created.')
            return redirect('user_list')
    return render(request, 'user_form.html', {'action': 'Create', 'role_choices': User.ROLE_CHOICES, 'dept_choices': User.DEPARTMENT_CHOICES})


@login_required(login_url='login')
@superuser_required
def user_edit_view(request, pk):
    edit_user = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        edit_user.first_name = request.POST.get('first_name', edit_user.first_name).strip()
        edit_user.last_name = request.POST.get('last_name', edit_user.last_name).strip()
        edit_user.email = request.POST.get('email', edit_user.email).strip()
        edit_user.role = request.POST.get('role', edit_user.role)
        edit_user.department = request.POST.get('department', edit_user.department)
        edit_user.phone = request.POST.get('phone', edit_user.phone).strip()
        edit_user.profile_notes = request.POST.get('profile_notes', edit_user.profile_notes).strip()
        edit_user.is_active = request.POST.get('is_active') == 'on'
        new_pw = request.POST.get('new_password', '').strip()
        if new_pw:
            if len(new_pw) < 8: messages.error(request, 'Password must be at least 8 characters.'); return render(request, 'user_form.html', {'action': 'Edit', 'edit_user': edit_user, 'role_choices': User.ROLE_CHOICES, 'dept_choices': User.DEPARTMENT_CHOICES})
            edit_user.set_password(new_pw)
        edit_user.save()
        log_activity(request.user, 'edit_user', f'Edited: {edit_user.username}', request)
        messages.success(request, f'User "{edit_user.username}" updated.')
        return redirect('user_list')
    return render(request, 'user_form.html', {'action': 'Edit', 'edit_user': edit_user, 'role_choices': User.ROLE_CHOICES, 'dept_choices': User.DEPARTMENT_CHOICES, 'activity_logs': edit_user.activity_logs.all()[:10]})


@login_required(login_url='login')
@superuser_required
def user_toggle_lock_view(request, pk):
    user = get_object_or_404(User, pk=pk)
    if user == request.user: messages.error(request, 'You cannot lock your own account.'); return redirect('user_list')
    user.is_locked = not user.is_locked
    user.failed_login_attempts = 0
    user.save(update_fields=['is_locked', 'failed_login_attempts'])
    messages.success(request, f'Account "{user.username}" {"locked" if user.is_locked else "unlocked"}.')
    return redirect('user_list')


@login_required(login_url='login')
@superuser_required
def user_activity_view(request, pk):
    user = get_object_or_404(User, pk=pk)
    return render(request, 'user_activity.html', {'target_user': user, 'logs': user.activity_logs.all()[:50]})


@login_required(login_url='login')
def profile_view(request):
    if request.method == 'POST':
        request.user.first_name = request.POST.get('first_name', request.user.first_name).strip()
        request.user.last_name = request.POST.get('last_name', request.user.last_name).strip()
        request.user.email = request.POST.get('email', request.user.email).strip()
        request.user.phone = request.POST.get('phone', request.user.phone).strip()
        new_pw = request.POST.get('new_password', '')
        if new_pw:
            if not request.user.check_password(request.POST.get('old_password', '')): messages.error(request, 'Current password is incorrect.'); return render(request, 'profile.html')
            if len(new_pw) < 8: messages.error(request, 'New password must be at least 8 characters.'); return render(request, 'profile.html')
            request.user.set_password(new_pw)
            update_session_auth_hash(request, request.user)
        request.user.save()
        messages.success(request, 'Profile updated.')
        return redirect('profile')
    return render(request, 'profile.html', {'recent_bookings': request.user.bookings.select_related('facility').order_by('-created_at')[:5], 'recent_activity': request.user.activity_logs.all()[:5]})


# ── Module 4: Notifications ───────────────────────────────────

@login_required(login_url='login')
def notifications_view(request):
    filter_type = request.GET.get('type', '')
    notifications = request.user.notifications.all()
    if filter_type:
        notifications = notifications.filter(notif_type=filter_type)

    # Mark all as read
    request.user.notifications.filter(is_read=False).update(is_read=True)

    unread_count = request.user.notifications.filter(is_read=False).count()
    total_count = request.user.notifications.count()

    # Recent announcements visible to everyone
    announcements = Announcement.objects.all()[:5]

    return render(request, 'notifications.html', {
        'notifications': notifications,
        'filter_type': filter_type,
        'type_choices': Notification.TYPE_CHOICES,
        'total_count': total_count,
        'announcements': announcements,
    })


@login_required(login_url='login')
def notification_mark_read(request, pk):
    notif = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notif.is_read = True
    notif.save()
    return redirect('notifications')


@login_required(login_url='login')
def notification_delete(request, pk):
    notif = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notif.delete()
    messages.success(request, 'Notification deleted.')
    return redirect('notifications')


@login_required(login_url='login')
@announcements_required
def announcement_create_view(request):
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        message = request.POST.get('message', '').strip()
        priority = request.POST.get('priority', 'normal')
        target_roles = request.POST.getlist('target_roles')

        if not title or not message:
            messages.error(request, 'Title and message are required.')
        else:
            roles_str = ','.join(target_roles)
            announcement = Announcement.objects.create(
                title=title, message=message, priority=priority,
                target_roles=roles_str, sent_by=request.user,
            )

            # Send to targeted users
            if target_roles:
                recipients = User.objects.filter(role__in=target_roles, is_active=True)
            else:
                recipients = User.objects.filter(is_active=True)

            count = 0
            for recipient in recipients:
                Notification.objects.create(
                    recipient=recipient, notif_type='announcement',
                    title=title, message=message, sent_by=request.user,
                )
                count += 1

            announcement.recipient_count = count
            announcement.save()
            messages.success(request, f'Announcement sent to {count} users.')
            return redirect('notifications')

    return render(request, 'announcement_form.html', {
        'role_choices': User.ROLE_CHOICES,
        'priority_choices': Announcement.PRIORITY_CHOICES,
    })


@login_required(login_url='login')
@announcements_required
def announcement_list_view(request):
    announcements = Announcement.objects.select_related('sent_by').all()
    return render(request, 'announcement_list.html', {'announcements': announcements})


@login_required(login_url='login')
@announcements_required
def template_list_view(request):
    templates = NotificationTemplate.objects.all()
    # Auto-create defaults if none exist
    defaults = [
        ('confirmation', 'Booking Confirmed: {facility}', 'Hi {user}, your booking for {facility} on {date} from {start_time} to {end_time} has been submitted and is pending approval.'),
        ('approval', 'Booking Approved: {facility}', 'Hi {user}, your booking for {facility} on {date} from {start_time} to {end_time} has been approved!'),
        ('rejection', 'Booking Rejected: {facility}', 'Hi {user}, unfortunately your booking for {facility} on {date} has been rejected.'),
        ('reminder', 'Reminder: Upcoming Booking', 'Hi {user}, this is a reminder that you have a booking for {facility} today at {start_time}.'),
        ('cancellation', 'Booking Cancelled: {facility}', 'Hi {user}, your booking for {facility} on {date} has been cancelled.'),
        ('escalation', 'Pending Approval Alert', 'A booking for {facility} on {date} has been pending approval for over 24 hours.'),
        ('announcement', 'Announcement', '{facility}'),
    ]
    for notif_type, subject, body in defaults:
        NotificationTemplate.objects.get_or_create(
            notif_type=notif_type,
            defaults={'subject': subject, 'body': body, 'updated_by': request.user}
        )
    templates = NotificationTemplate.objects.all()
    return render(request, 'notification_templates.html', {'templates': templates})


@login_required(login_url='login')
@announcements_required
def template_edit_view(request, pk):
    template = get_object_or_404(NotificationTemplate, pk=pk)
    if request.method == 'POST':
        template.subject = request.POST.get('subject', template.subject).strip()
        template.body = request.POST.get('body', template.body).strip()
        template.is_active = request.POST.get('is_active') == 'on'
        template.updated_by = request.user
        template.save()
        messages.success(request, f'Template "{template.get_notif_type_display()}" updated.')
        return redirect('notification_templates')
    return render(request, 'template_form.html', {'template': template})


# ── Module 5: Reports & Analytics ────────────────────────────

@login_required(login_url='login')
@reports_required
def reports_view(request):
    from django.db.models import Count, Q

    date_from_str = request.GET.get('date_from', '')
    date_to_str = request.GET.get('date_to', '')
    facility_type_filter = request.GET.get('facility_type', '')
    floor_filter = request.GET.get('floor', '')
    today = datetime.date.today()
    thirty_days_ago = today - datetime.timedelta(days=30)
    date_from = datetime.date.fromisoformat(date_from_str) if date_from_str else thirty_days_ago
    date_to = datetime.date.fromisoformat(date_to_str) if date_to_str else today
    bookings_qs = Booking.objects.filter(date__gte=date_from, date__lte=date_to)
    if facility_type_filter: bookings_qs = bookings_qs.filter(facility__facility_type=facility_type_filter)
    if floor_filter: bookings_qs = bookings_qs.filter(facility__floor=floor_filter)
    total_bookings = bookings_qs.count()
    approved = bookings_qs.filter(status='approved').count()
    pending = bookings_qs.filter(status='pending').count()
    rejected = bookings_qs.filter(status='rejected').count()
    cancelled = bookings_qs.filter(status='cancelled').count()
    approval_rate = round((approved / total_bookings * 100) if total_bookings else 0)
    facility_usage = list(Facility.objects.annotate(total=Count('bookings', filter=Q(bookings__date__gte=date_from, bookings__date__lte=date_to)), approved_count=Count('bookings', filter=Q(bookings__status='approved', bookings__date__gte=date_from, bookings__date__lte=date_to))).filter(total__gt=0).order_by('-total').values('name', 'floor', 'facility_type', 'total', 'approved_count')[:10])
    status_chart = {'labels': ['Approved', 'Pending', 'Rejected', 'Cancelled'], 'data': [approved, pending, rejected, cancelled], 'colors': ['#1a6b3a', '#b7770d', '#c0392b', '#888780']}
    daily_labels, daily_data = [], []
    for i in range(13, -1, -1):
        d = today - datetime.timedelta(days=i)
        daily_labels.append(d.strftime('%b %d'))
        daily_data.append(Booking.objects.filter(date=d).count())
    hour_data = [Booking.objects.filter(start_time__gte=datetime.time(h, 0), start_time__lte=datetime.time(h, 59), status__in=['approved', 'pending']).count() for h in range(7, 21)]
    type_data = list(Booking.objects.filter(date__gte=date_from, date__lte=date_to).values('facility__facility_type').annotate(count=Count('id')).order_by('-count'))
    floor_data = list(Booking.objects.filter(date__gte=date_from, date__lte=date_to).values('facility__floor').annotate(count=Count('id')).order_by('facility__floor'))
    top_bookers = list(Booking.objects.filter(date__gte=date_from, date__lte=date_to).values('booked_by__first_name', 'booked_by__last_name', 'booked_by__username', 'booked_by__role').annotate(count=Count('id')).order_by('-count')[:5])
    context = {'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(), 'facility_type_filter': facility_type_filter, 'floor_filter': floor_filter, 'floor_choices': Facility.FLOOR_CHOICES, 'type_choices': Facility.TYPE_CHOICES, 'total_bookings': total_bookings, 'approved': approved, 'pending': pending, 'rejected': rejected, 'cancelled': cancelled, 'approval_rate': approval_rate, 'total_facilities': Facility.objects.count(), 'active_facilities': Facility.objects.filter(status='active').count(), 'maintenance_facilities': Facility.objects.filter(status='maintenance').count(), 'total_users': User.objects.count(), 'status_chart_json': json.dumps(status_chart), 'daily_labels_json': json.dumps(daily_labels), 'daily_data_json': json.dumps(daily_data), 'hour_labels_json': json.dumps([f'{h}:00' for h in range(7, 21)]), 'hour_data_json': json.dumps(hour_data), 'type_chart_json': json.dumps({'labels': [t['facility__facility_type'].replace('_', ' ').title() for t in type_data], 'data': [t['count'] for t in type_data]}), 'floor_chart_json': json.dumps({'labels': [f['facility__floor'] or 'Unknown' for f in floor_data], 'data': [f['count'] for f in floor_data]}), 'facility_usage': facility_usage, 'top_bookers': top_bookers}
    return render(request, 'reports.html', context)


@login_required(login_url='login')
@reports_required
def reports_export_csv(request):
    date_from_str = request.GET.get('date_from', '')
    date_to_str = request.GET.get('date_to', '')
    today = datetime.date.today()
    date_from = datetime.date.fromisoformat(date_from_str) if date_from_str else today - datetime.timedelta(days=30)
    date_to = datetime.date.fromisoformat(date_to_str) if date_to_str else today
    bookings = Booking.objects.filter(date__gte=date_from, date__lte=date_to).select_related('facility', 'booked_by', 'approved_by').order_by('date', 'start_time')
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="bookings_{date_from}_{date_to}.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Facility', 'Floor', 'Type', 'Booked By', 'Role', 'Date', 'Start', 'End', 'Attendees', 'Purpose', 'Status', 'Approved By'])
    for b in bookings:
        writer.writerow([b.pk, b.facility.name, b.facility.floor, b.facility.get_facility_type_display(), b.booked_by.get_full_name() or b.booked_by.username, b.booked_by.get_role_display(), b.date, b.start_time.strftime('%H:%M'), b.end_time.strftime('%H:%M'), b.number_of_attendees, b.purpose, b.get_status_display(), b.approved_by.get_full_name() if b.approved_by else ''])
    return response