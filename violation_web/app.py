"""
Main Flask application for the violation management system.

This application exposes a simple web interface that wraps the functionality
of your original Excel workbook.  It provides login/registration, a summary
view with filters, complaint handling, payment instructions with QR code,
and an administrative backend for editing records.  All data is persisted
to a local SQLite database and the original Excel file is only used to
initialise the database on the first run.

To run the application locally you will need to install the dependencies
listed in ``requirements.txt`` (see README for details).  Once everything
is installed you can start the server with ``python app.py`` and visit
``http://localhost:5000`` in your browser.

Note: This file assumes it lives in the ``violation_web`` folder with
``utils.py``, ``models.py`` and a ``templates`` directory alongside it.
"""

import os
from datetime import datetime, timedelta
from typing import Optional, List

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

from .utils import (
    import_excel_if_needed, compute_week_number, format_currency,
    generate_payment_message, generate_qr_code_base64
)
from .utils import update_excel_payment
from .utils import remove_violation_from_excel
from werkzeug.utils import secure_filename

from .utils import get_ds_lop_names

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, 'database.db')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'replace-me')

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    # Khi chạy trên Render (PostgreSQL)
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
else:
    # Khi chạy local (SQLite)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DATABASE_PATH}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)


# -----------------------------------------------------------------------------
# Database Models
# -----------------------------------------------------------------------------

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    display_name = db.Column(db.String(128), nullable=False)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(256), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Link this user to a student from DS_LOP.  Initially None until
    # selected the first time the user attempts to make a payment.
    student_name = db.Column(db.String(128), nullable=True)

    # Optional path to the user's avatar image stored under
    # ``static/images/avatars``.  When ``None`` a default icon is shown.
    avatar_path = db.Column(db.String(256), nullable=True)

    # Optional biography/description for the user.  This is displayed on
    # the profile page and can be edited by the user.
    bio = db.Column(db.String(512), nullable=True)

    payments = db.relationship('Payment', backref='user', lazy=True)
    complaints = db.relationship('Complaint', backref='user', lazy=True)

    # Each user can receive many notifications.  When new violations are
    # recorded or a user registers, a notification is created and linked
    # to the target user.  Notifications are displayed in the navigation
    # bar and on the dedicated notifications page.
    notifications = db.relationship('Notification', backref='user', lazy=True)

    # Distinguish between super administrators (full privileges) and
    # regular administrators (limited privileges).  A super admin can
    # manage user roles and delete accounts, whereas regular admins
    # cannot.  By default new users are not super admins.
    is_super_admin = db.Column(db.Boolean, default=False)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class ViolationRecord(db.Model):
    """
    Represents a single violation record imported from the Excel workbook.

    ``sheet_name`` stores the worksheet source (e.g. ``NHAT_KI_DI_MUON``),
    ``week`` stores the ISO week number, ``date`` stores the calendar date,
    and ``error_code`` references a code defined in ``ErrorCode``.  ``amount_due``
    and ``amount_paid`` track finances separately, while ``notes`` holds any
    free‑form comments.  ``created_at`` is used to sort records chronologically.
    """
    __tablename__ = 'violation_records'
    id = db.Column(db.Integer, primary_key=True)
    sheet_name = db.Column(db.String(64), nullable=False)
    week = db.Column(db.Integer, nullable=False)
    date = db.Column(db.Date, nullable=False)
    student_name = db.Column(db.String(128), nullable=False)
    error_code = db.Column(db.String(8), nullable=False)
    reason = db.Column(db.String(512), nullable=True)
    amount_due = db.Column(db.Integer, nullable=False, default=0)
    amount_paid = db.Column(db.Integer, nullable=False, default=0)
    payment_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.String(512), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def unpaid_amount(self) -> int:
        return max(self.amount_due - self.amount_paid, 0)


class Payment(db.Model):
    __tablename__ = 'payments'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    error_code = db.Column(db.String(8), nullable=False)
    transfer_date = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.String(512), nullable=True)


class Complaint(db.Model):
    __tablename__ = 'complaints'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    violation_id = db.Column(db.Integer, db.ForeignKey('violation_records.id'), nullable=False)
    error_code = db.Column(db.String(8), nullable=False)
    target_student = db.Column(db.String(128), nullable=False)
    target_error = db.Column(db.String(256), nullable=False)
    complaint_email = db.Column(db.String(256), nullable=False)
    message = db.Column(db.Text, nullable=False)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved = db.Column(db.Boolean, default=False)


class ErrorCode(db.Model):
    """
    Keeps track of known error codes and their human‑readable descriptions.  When
    adding a new type of violation this table should be updated by an admin.
    """
    __tablename__ = 'error_codes'
    code = db.Column(db.String(8), primary_key=True)
    description = db.Column(db.String(256), nullable=False)
    default_amount = db.Column(db.Integer, nullable=False, default=0)


# New model: Notification
class Notification(db.Model):
    """
    Represents a user‑specific message.  Notifications are created when
    significant events occur, such as when a new violation is recorded
    against a student with an associated account or when a user
    registers for the first time.  Each notification has a message and
    optionally a URL that the user can follow for more information.
    The ``is_read`` flag marks whether the notification has been seen.
    """
    __tablename__ = 'notifications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    message = db.Column(db.String(512), nullable=False)
    url = db.Column(db.String(256), nullable=True)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# New model: VisitCount
class VisitCount(db.Model):
    """
    Tracks the total number of page visits across the entire system.
    There is always exactly one row in this table whose ``count``
    increments each time a page is loaded.  The value is displayed
    as part of the dashboard metrics on the home page.
    """
    __tablename__ = 'visit_count'
    id = db.Column(db.Integer, primary_key=True)
    count = db.Column(db.Integer, default=0)


# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------

def login_required(fn):
    """Decorator that redirects anonymous users to the login page."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('user_id'):
            flash('Bạn phải đăng nhập để xem trang này.', 'warning')
            return redirect(url_for('login', next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def get_current_user() -> Optional[User]:
    user_id = session.get('user_id')
    if user_id:
        return User.query.get(int(user_id))
    return None


def increment_visit() -> None:
    """
    Increment the global visit counter for a unique visitor.  A visit
    is counted only when the user first opens the home page from a
    particular IP or browser session.  Subsequent page views within
    the same session do not increment the counter.  The flag
    ``visit_recorded`` is stored in the session to avoid double
    counting.
    
    This helper should only be called from the ``index`` route.  If
    called elsewhere it will silently return without altering the
    counter.  This prevents erroneously incrementing the count when
    navigating to sub‑pages.
    """
    try:
        # Do nothing if a visit has already been recorded for this session
        if session.get('visit_recorded'):
            return
        vc = VisitCount.query.first()
        if vc:
            vc.count += 1
            db.session.commit()
        session['visit_recorded'] = True
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Context processors
# -----------------------------------------------------------------------------
@app.context_processor
def inject_nav_metrics() -> dict:
    """
    Inject a few globally useful variables into all templates.  In
    particular we compute the current user's outstanding debt (if
    applicable) and the number of unresolved complaints (if the user is
    an administrator).  These values are used in the navigation bar to
    display notification badges.
    """
    user = get_current_user()
    outstanding = None
    notifications_count = None
    visit_count = None
    nav_notification_list = []
    if user:
        # Compute outstanding only if the student name is set
        if user.student_name:
            records = ViolationRecord.query.filter_by(student_name=user.student_name).all()
            outstanding = sum(r.unpaid_amount() for r in records)
        # Determine notifications for the navigation dropdown.  Admins
        # see all notifications; normal users see only their own.  We
        # no longer limit the number returned so that users can see
        # all recent activity.  Notifications are ordered newest first.
        if user.is_admin:
            notes_query = Notification.query.order_by(Notification.created_at.desc())
            # Admins count unread across all users to highlight pending items
            notifications_count = Notification.query.filter_by(is_read=False).count()
        else:
            notes_query = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc())
            notifications_count = Notification.query.filter_by(user_id=user.id, is_read=False).count()
        notes = notes_query.all()
        nav_notification_list = [
            {
                'id': n.id,
                'message': n.message,
                'url': n.url or '',
                'is_read': n.is_read,
                'created_at': n.created_at
            }
            for n in notes
        ]
    # Always include total visits in the nav; show to everyone for the
    # dashboard card but not necessarily as a badge
    vc = VisitCount.query.first()
    if vc:
        visit_count = vc.count
    return {
        'nav_outstanding': outstanding,
        'nav_notifications': notifications_count,
        'nav_visit_count': visit_count,
        'nav_notification_list': nav_notification_list if user else None
    }


# -----------------------------------------------------------------------------
# Startup logic
# -----------------------------------------------------------------------------

def initialise_database() -> None:
    db.create_all()

    if ErrorCode.query.count() == 0:
        codes = [
            ('VP01', 'Đi muộn', 10000),
            ('VP02', 'Cho người lạ vào lớp', 0),
            ('VP03', 'Đổi chỗ', 10000),
            ('VP04', 'Quên đồ dùng học tập', 10000),
            ('VP05', 'Ngủ trong giờ học', 10000),
            ('VP06', 'Nghỉ học vô lí do', 30000),
        ]
        for code, desc, amt in codes:
            db.session.add(ErrorCode(code=code, description=desc, default_amount=amt))
        db.session.commit()

    if ViolationRecord.query.count() == 0:
        excel_path = os.path.join(BASE_DIR, '..', 'Danh Sách Vi Phạm .xlsm')
        import_excel_if_needed(excel_path, db, ErrorCode, ViolationRecord)
        db.session.commit()

    """
    Ensures the database tables exist and imports the Excel data on the
    first run.  If ``database.db`` already contains violation records
    the import step is skipped.
    """
    db.create_all()
    # Ensure new columns (such as student_name, avatar_path and bio) exist.
    # SQLite does not automatically add columns on model changes, so
    # perform a manual ALTER if necessary.
    insp = db.inspect(db.engine)
    existing_cols = [c['name'] for c in insp.get_columns('users')]
    with db.engine.connect() as conn:
        if 'student_name' not in existing_cols:
            conn.execute(db.text('ALTER TABLE users ADD COLUMN student_name VARCHAR(128)'))
        if 'avatar_path' not in existing_cols:
            conn.execute(db.text('ALTER TABLE users ADD COLUMN avatar_path VARCHAR(256)'))
        if 'bio' not in existing_cols:
            conn.execute(db.text('ALTER TABLE users ADD COLUMN bio VARCHAR(512)'))
    # load error codes if none exist
    if ErrorCode.query.count() == 0:
        # Preload default codes based on the Excel workbook specification
        codes = [
            ('VP01', 'Đi muộn', 10000),
            ('VP02', 'Cho người lạ vào lớp', 0),
            ('VP03', 'Đổi chỗ', 10000),
            ('VP04', 'Quên đồ dùng học tập', 10000),
            ('VP05', 'Ngủ trong giờ học', 10000),
            ('VP06', 'Nghỉ học vô lí do', 30000),
        ]
        for code, desc, amt in codes:
            db.session.add(ErrorCode(code=code, description=desc, default_amount=amt))
        db.session.commit()
    # import violation records if table empty
    if ViolationRecord.query.count() == 0:
        excel_path = os.path.join(BASE_DIR, '..', 'Danh Sách Vi Phạm .xlsm')
        import_excel_if_needed(excel_path, db, ErrorCode, ViolationRecord)
        db.session.commit()

    # Ensure a visit counter row exists.  The VisitCount table
    # maintains a single record that stores the total number of page
    # visits.  If there are no rows present we insert one.
    if VisitCount.query.count() == 0:
        vc = VisitCount(count=0)
        db.session.add(vc)
        db.session.commit()

    # Load DS_LOP names and store in app config for later use (payment selection)
    excel_path = os.path.join(BASE_DIR, '..', 'Danh Sách Vi Phạm .xlsm')
    try:
        app.config['DS_LOP_NAMES'] = get_ds_lop_names(excel_path)
    except Exception:
        app.config['DS_LOP_NAMES'] = []

    # Make format_currency globally available to all templates
    app.jinja_env.globals.update(format_currency=format_currency)


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.route('/')
def index() -> str:
    # Increment visit count once per session on the home page
    increment_visit()
    user = get_current_user()
    outstanding = None
    # Determine local date/time in Asia/Bangkok (UTC+7) for week and other calculations
    # Instead of using UTC we offset by 7 hours.  This ensures week numbers
    # and dates reflect the Hanoi timezone.
    now_local = datetime.utcnow() + timedelta(hours=7)
    today_local = now_local.date()
    # compute outstanding debt for the logged in user if they have
    # associated a student name.  This value is used to prompt
    # individuals to pay outstanding fines on the home page.
    if user and user.student_name:
        records = ViolationRecord.query.filter_by(student_name=user.student_name).all()
        outstanding = sum(r.unpaid_amount() for r in records)
    # Total number of students: fixed at 35 or number of names in DS_LOP
    try:
        ds_names = app.config.get('DS_LOP_NAMES', [])
        total_students = max(35, len(ds_names))
    except Exception:
        total_students = 35
    # Compute financial totals: we use dynamic penalty calculation for
    # repeated offences of VP01 and VP06.  For each student, week and
    # error code we add 10,000 VND for each additional occurrence in
    # that week.  The total fines equal the sum of these dynamic due
    # amounts across all records; total paid is the sum of amount_paid,
    # and outstanding is the difference.
    all_records = ViolationRecord.query.all()
    from collections import defaultdict
    # Precompute default amounts for error codes
    error_defaults = {e.code: e.default_amount for e in ErrorCode.query.all()}
    # Build dynamic due map for all records
    dynamic_due_map: dict[int, int] = {}
    # Group by student, week and error code
    per_key: defaultdict[tuple, list] = defaultdict(list)
    for rec in all_records:
        per_key[(rec.student_name, rec.week, rec.error_code)].append(rec)
    for (student_name, wk, code), rec_list in per_key.items():
        rec_list_sorted = sorted(rec_list, key=lambda x: (x.date, x.id))
        base_amount = error_defaults.get(code, 0)
        for idx, rr in enumerate(rec_list_sorted):
            if code in ('VP01', 'VP06'):
                dyn_due = base_amount + 10000 * idx
            else:
                dyn_due = base_amount
            dynamic_due_map[rr.id] = dyn_due
    # Calculate totals using dynamic due map
    overall_total_due = sum(dynamic_due_map.get(r.id, r.amount_due) for r in all_records)
    overall_total_paid = sum(r.amount_paid for r in all_records)
    overall_total_outstanding = overall_total_due - overall_total_paid
    total_fines = overall_total_due
    # Determine the current week number using the custom calendar (start from
    # 8 Sept 2025 and skip the Tết break).  Use local date for week calculation.
    from .utils import compute_custom_week
    current_week = compute_custom_week(today_local)
    violations_this_week = ViolationRecord.query.filter_by(week=current_week).count()
    # Retrieve total page visits from the VisitCount table.
    vc = VisitCount.query.first()
    total_visits = vc.count if vc else 0
    # Build metrics for dashboard
    metrics = [
        {
            'icon': '👥',
            'label': 'Tổng Số Học Sinh',
            'value': total_students,
            'color': '#4ade80'
        },
        {
            'icon': '📄',
            'label': f'Số Vi Phạm Tuần {current_week}',
            'value': violations_this_week,
            'color': '#f87171'
        },
        {
            'icon': '💸',
            'label': 'Tổng Tiền Phạt',
            'value': format_currency(total_fines),
            'color': '#facc15'
        },
        {
            'icon': '👀',
            'label': 'Lượt Truy Cập',
            'value': total_visits,
            'color': '#fb923c'
        }
    ]
    # Prepare chart data: number of violations per week across all students.
    chart_labels = None
    chart_values = None
    if all_records:
        from collections import defaultdict
        week_counts = defaultdict(int)
        for rec in all_records:
            week_counts[rec.week] += 1
        sorted_weeks = sorted(week_counts.keys())
        chart_labels = [w for w in sorted_weeks]
        chart_values = [week_counts[w] for w in sorted_weeks]
    # Prepare summary table for the current week
    week_summary = []
    current_week_records = ViolationRecord.query.filter_by(week=current_week).all()
    if current_week_records:
        from collections import defaultdict
        tmp = defaultdict(list)
        for rec in current_week_records:
            tmp[rec.student_name].append(rec)
        for student_name, recs in tmp.items():
            count = len(recs)
            # Compute dynamic due/outstanding for this student's records in the current week
            dyn_due_map: dict[int, int] = {}
            dyn_out_map: dict[int, int] = {}
            per_code: defaultdict[str, list] = defaultdict(list)
            for r in recs:
                per_code[r.error_code].append(r)
            for code, rec_list in per_code.items():
                sorted_list = sorted(rec_list, key=lambda x: (x.date, x.id))
                base = error_defaults.get(code, 0)
                for idx, rr in enumerate(sorted_list):
                    if code in ('VP01', 'VP06'):
                        ddue = base + 10000 * idx
                    else:
                        ddue = base
                    dyn_due_map[rr.id] = ddue
                    out_amt = ddue - rr.amount_paid
                    dyn_out_map[rr.id] = out_amt if out_amt > 0 else 0
            due = sum(dyn_due_map.get(r.id, r.amount_due) for r in recs)
            paid = sum(r.amount_paid for r in recs)
            outstanding_amt = sum(dyn_out_map.get(r.id, r.unpaid_amount()) for r in recs)
            week_summary.append({
                'student': student_name,
                'count': count,
                'due': due,
                'paid': paid,
                'outstanding': outstanding_amt
            })
        week_summary = sorted(week_summary, key=lambda x: x['count'], reverse=True)
    # Select fixed images for the home page gallery.  We no longer
    # randomise the gallery; instead we choose a curated set of images
    # (design1, design2, design3) so that the layout remains consistent.
    gallery_imgs = []
    try:
        image_dir = os.path.join(BASE_DIR, 'static', 'images')
        # list of preferred images in order
        preferred = ['design1.png', 'design2.png', 'design3.png']
        for img in preferred:
            if os.path.exists(os.path.join(image_dir, img)):
                gallery_imgs.append(img)
    except Exception:
        gallery_imgs = []
    return render_template(
        'index.html',
        user=user,
        outstanding=outstanding,
        metrics=metrics,
        chart_labels=chart_labels,
        chart_values=chart_values,
        week_summary=week_summary,
        current_week=current_week,
        breadcrumbs=[{'label': 'Trang Chủ', 'url': url_for('index')}],
        gallery_imgs=gallery_imgs
    )


@app.route('/register', methods=['GET', 'POST'])
def register() -> str:
    if request.method == 'POST':
        display_name = request.form.get('display_name', '').strip()
        username = request.form.get('username', '').strip().lower()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        # basic validation
        if not display_name or not username or not email or not password:
            flash('Vui lòng điền đầy đủ các trường.', 'danger')
            return render_template('register.html')
        if password != password2:
            flash('Mật khẩu xác nhận không khớp.', 'danger')
            return render_template('register.html')
        if not username.isalnum():
            flash('Tên người dùng chỉ được chứa chữ và số, không có kí tự đặc biệt.', 'danger')
            return render_template('register.html')
        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash('Tên người dùng hoặc email đã tồn tại.', 'danger')
            return render_template('register.html')
        # create user
        user = User(display_name=display_name, username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        # Create a welcome notification for the new user
        welcome_msg = f'Chào mừng {display_name}! Tài khoản của bạn đã được tạo thành công.'
        note = Notification(user_id=user.id, message=welcome_msg, url=url_for('index'))
        db.session.add(note)
        db.session.commit()
        flash('Tạo tài khoản thành công. Bạn có thể đăng nhập.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login() -> str:
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        user: Optional[User] = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session['user_id'] = user.id
            flash('Đăng nhập thành công.', 'success')
            next_url = request.args.get('next')
            return redirect(next_url or url_for('summary'))
        flash('Tên người dùng hoặc mật khẩu không đúng.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout() -> str:
    session.pop('user_id', None)
    flash('Đã đăng xuất.', 'info')
    return redirect(url_for('index'))


@app.route('/summary')
def summary() -> str:
    user = get_current_user()
    # gather filters from query parameters
    week_str = request.args.get('week')
    day_str = request.args.get('day')
    student = request.args.get('student')
    error = request.args.get('error_code')
    # build base query applying filters on fields except payment status.  We will
    # apply payment status after computing dynamic penalties so that the
    # comparison is based on the true amount due rather than the static
    # ``amount_due`` field.  Start with all records and narrow down.
    query = ViolationRecord.query
    # Filter by week number
    if week_str:
        try:
            w = int(week_str)
            query = query.filter_by(week=w)
        except ValueError:
            pass
    # Filter by exact date
    if day_str:
        try:
            date_filter = datetime.strptime(day_str, '%Y-%m-%d').date()
            query = query.filter_by(date=date_filter)
        except ValueError:
            pass
    # Filter by student name
    if student:
        # use case-insensitive match for convenience
        query = query.filter(ViolationRecord.student_name.ilike(f'%{student}%'))
    # Filter by error code
    if error:
        query = query.filter_by(error_code=error)

    # At this point we have a base set of records matching week/date/student/error.
    # We now need to compute dynamic penalties across **all** violation records
    # so that repeated offences (VP01 and VP06) accrue extra fines.  The
    # dynamic penalty does not depend on the filter, so we compute it for
    # every record in the database.  Afterwards we will apply the payment
    # status filter (paid/unpaid) using this dynamic penalty.
    all_recs = ViolationRecord.query.all()
    # Precompute default penalty for each error code
    error_defaults = {e.code: e.default_amount for e in ErrorCode.query.all()}
    from collections import defaultdict
    # Compute dynamic due for each record across the entire dataset
    dynamic_due_all: dict[int, int] = {}
    # Group all records by (student, week, error code)
    per_key_all: defaultdict[tuple, list] = defaultdict(list)
    for rec in all_recs:
        per_key_all[(rec.student_name, rec.week, rec.error_code)].append(rec)
    for (stu, wk, code), rec_list in per_key_all.items():
        sorted_list = sorted(rec_list, key=lambda r: (r.date, r.id))
        base_amt = error_defaults.get(code, 0)
        for idx, rr in enumerate(sorted_list):
            if code in ('VP01', 'VP06'):
                dynamic_due_all[rr.id] = base_amt + 10000 * idx
            else:
                dynamic_due_all[rr.id] = base_amt
    # Now fetch the filtered records from the base query
    if not user and not week_str:
        # If not logged in, require a week selection
        flash('Bạn phải chọn tuần để xem dữ liệu nếu chưa đăng nhập.', 'info')
        preliminary_records = []
    else:
        preliminary_records = query.order_by(ViolationRecord.date.desc()).all()
    # Apply payment status filter based on dynamic penalties
    status = request.args.get('status')
    records = []
    if status == 'paid':
        # include only records where dynamic due is less than or equal to amount_paid
        records = [r for r in preliminary_records if dynamic_due_all.get(r.id, r.amount_due) <= r.amount_paid]
    elif status == 'unpaid':
        # include only records where dynamic due is greater than amount_paid
        records = [r for r in preliminary_records if dynamic_due_all.get(r.id, r.amount_due) > r.amount_paid]
    else:
        records = preliminary_records
    # Gather unique values for filters
    all_recs = ViolationRecord.query.all()
    weeks = sorted({r.week for r in all_recs})
    students = sorted({r.student_name for r in all_recs})
    errors = ErrorCode.query.all()
    # Precompute default penalty for each error code (for display descriptions)
    error_defaults = {e.code: e.default_amount for e in errors}
    error_dict = {e.code: e.description for e in errors}
    # Group filtered records by student and compute totals using dynamic penalties
    from collections import defaultdict
    grouped: defaultdict[str, list] = defaultdict(list)
    for rec in records:
        grouped[rec.student_name].append(rec)
    grouped_records = []
    overall_total_due = 0
    overall_total_paid = 0
    overall_total_outstanding = 0
    for student_name, recs in grouped.items():
        total_count = len(recs)
        total_due = 0
        total_paid = 0
        total_outstanding = 0
        enumerated_records = []
        for idx, rec in enumerate(sorted(recs, key=lambda r: (r.date, r.id))):
            # Use the globally computed dynamic penalty for this record
            dyn_due = dynamic_due_all.get(rec.id, rec.amount_due)
            outstanding = dyn_due - rec.amount_paid
            if outstanding < 0:
                outstanding = 0
            total_due += dyn_due
            total_paid += rec.amount_paid
            total_outstanding += outstanding
            enumerated_records.append({'index': idx + 1, 'record': rec})
        overall_total_due += total_due
        overall_total_paid += total_paid
        overall_total_outstanding += total_outstanding
        grouped_records.append({
            'student': student_name,
            'records': enumerated_records,
            'total_count': total_count,
            'total_due': total_due,
            'total_paid': total_paid,
            'total_outstanding': total_outstanding,
        })
    return render_template(
        'summary.html', user=user, groups=grouped_records, weeks=weeks,
        students=students, errors=errors, selected_week=week_str,
        selected_day=day_str, selected_student=student, selected_error=error,
        format_currency=format_currency, error_dict=error_dict,
        overall_total_due=overall_total_due,
        overall_total_paid=overall_total_paid,
        overall_total_outstanding=overall_total_outstanding,
        selected_status=status,
        breadcrumbs=[
            {'label': 'Trang Chủ', 'url': url_for('index')},
            {'label': 'Bảng Tổng Hợp', 'url': url_for('summary')}
        ]
    )


@app.route('/complaint/<int:record_id>', methods=['GET', 'POST'])
@login_required
def complaint(record_id: int) -> str:
    user = get_current_user()
    record: Optional[ViolationRecord] = ViolationRecord.query.get_or_404(record_id)
    if request.method == 'POST':
        error_code = request.form.get('error_code') or record.error_code
        email = request.form.get('email', '').strip()
        message = request.form.get('message', '').strip()
        if not email or not message:
            flash('Vui lòng nhập email và nội dung khiếu nại.', 'danger')
        else:
            comp = Complaint(
                user_id=user.id,
                violation_id=record.id,
                error_code=error_code,
                target_student=record.student_name,
                target_error=ErrorCode.query.get(error_code).description,
                complaint_email=email,
                message=message
            )
            db.session.add(comp)
            db.session.commit()
            flash('Đã gửi khiếu nại. Quản trị viên sẽ xem xét.', 'success')
            return redirect(url_for('summary'))
    # possible error codes for complaint list
    codes = ErrorCode.query.all()
    return render_template(
        'complaint.html', user=user, record=record, codes=codes,
        breadcrumbs=[
            {'label': 'Trang Chủ', 'url': url_for('index')},
            {'label': 'Bảng Tổng Hợp', 'url': url_for('summary')},
            {'label': 'Khiếu Nại', 'url': url_for('complaint', record_id=record_id)}
        ]
    )


@app.route('/pay/<string:username>', methods=['GET', 'POST'])
@login_required
def pay(username: str) -> str:
    """
    Payment page.  On the first visit by a normal user the system asks the
    user to choose the student record (học sinh) they want to pay for.  Once
    selected the choice is saved in ``User.student_name`` and subsequent
    visits jump directly to the QR/instructions view.  Administrators can
    specify any username in the URL to view the payment info for that user.
    """
    current_user = get_current_user()
    # Determine which account we are paying on behalf of.  For admins the
    # ``username`` parameter identifies the user account, but the admin
    # can choose any student to pay for on each visit.  For normal
    # users the username must match the current user.
    if current_user.is_admin:
        target_user = User.query.filter_by(username=username).first()
        if not target_user:
            flash('Không tìm thấy tài khoản người dùng.', 'danger')
            return redirect(url_for('summary'))
    else:
        if current_user.username != username:
            flash('Bạn không được phép thanh toán cho người khác.', 'danger')
            return redirect(url_for('summary'))
        target_user = current_user
    # For administrators we always ask to select the student name on every
    # visit.  The selected name is passed via a query parameter so that
    # the pay page can display the outstanding amount for that student.
    if current_user.is_admin:
        selected_student = request.args.get('student')
        ds_names = app.config.get('DS_LOP_NAMES', [])
        if not selected_student:
            # Render a selection form for admin to choose a student.  Use
            # pay_select template and pass a flag to differentiate admin view.
            return render_template(
                'pay_select.html', user=current_user, names=ds_names, admin_select=True,
                breadcrumbs=[
                    {'label': 'Trang Chủ', 'url': url_for('index')},
                    {'label': 'Nộp Tiền', 'url': url_for('pay', username=username)}
                ]
            )
        # Use the provided student name for calculations; do not persist
        student_name = selected_student
    else:
        # For normal users, ensure they have selected a student record
        if not target_user.student_name:
            ds_names = app.config.get('DS_LOP_NAMES', [])
            if request.method == 'POST':
                selected = request.form.get('student_name')
                if selected:
                    target_user.student_name = selected
                    db.session.commit()
                    flash('Đã lưu tên học sinh. Tiếp tục thanh toán.', 'success')
                    return redirect(url_for('pay', username=username))
                else:
                    flash('Vui lòng chọn tên học sinh.', 'danger')
            return render_template(
                'pay_select.html', user=current_user, names=ds_names, admin_select=False,
                breadcrumbs=[
                    {'label': 'Trang Chủ', 'url': url_for('index')},
                    {'label': 'Nộp Tiền', 'url': url_for('pay', username=username)}
                ]
            )
        # Normal user uses their saved student name
        student_name = target_user.student_name
    # Compute total unpaid using dynamic penalties.  For each record,
    # determine the dynamic due amount based on how many times the student
    # has committed the same violation within the same week.  Sum the
    # outstanding amounts (dynamic due minus amount_paid) across all
    # records for this student.  Also collect the list of error codes
    # where there is an outstanding balance.
    records = ViolationRecord.query.filter_by(student_name=student_name).all()
    # Compute dynamic due for all records across the database
    all_recs = ViolationRecord.query.all()
    error_defaults = {e.code: e.default_amount for e in ErrorCode.query.all()}
    from collections import defaultdict
    dynamic_due_all: dict[int, int] = {}
    per_key_all: defaultdict[tuple, list] = defaultdict(list)
    for rec in all_recs:
        per_key_all[(rec.student_name, rec.week, rec.error_code)].append(rec)
    for (stu, wk, code), rec_list in per_key_all.items():
        sorted_list = sorted(rec_list, key=lambda r: (r.date, r.id))
        base_amt = error_defaults.get(code, 0)
        for idx, rr in enumerate(sorted_list):
            if code in ('VP01', 'VP06'):
                dynamic_due_all[rr.id] = base_amt + 10000 * idx
            else:
                dynamic_due_all[rr.id] = base_amt
    total_unpaid = 0
    codes_set = set()
    for rec in records:
        dyn_due = dynamic_due_all.get(rec.id, rec.amount_due)
        outstanding_amt = dyn_due - rec.amount_paid
        if outstanding_amt > 0:
            total_unpaid += outstanding_amt
            codes_set.add(rec.error_code)
    codes = sorted(codes_set)
    payment_message = generate_payment_message(student_name, total_unpaid, codes)
    qr_image = generate_qr_code_base64(payment_message)
    instructions = [
        'Bước 1: Quét mã QR bên cạnh bằng ứng dụng Momo hoặc ngân hàng. Bạn cũng có thể truy cập trực tiếp bằng liên kết: https://quy.momo.vn/v2/AJB9sUnYjt',
        'Bước 2: Nhập nội dung chuyển khoản theo mẫu: Họ và tên + Số tiền nộp + Mã lỗi (Nếu nhiều mã giống nhau chỉ ghi một lần).',
        'Lưu ý: Vui lòng nộp đủ số tiền trong một lần và đảm bảo nội dung chính xác.'
    ]
    return render_template(
        'pay.html', user=current_user, total_unpaid=total_unpaid,
        codes=codes, payment_message=payment_message, qr_image=qr_image,
        instructions=instructions, student_name=student_name,
        admin_select=current_user.is_admin,
        breadcrumbs=[
            {'label': 'Trang Chủ', 'url': url_for('index')},
            {'label': 'Nộp Tiền', 'url': url_for('pay', username=username)}
        ]
    )


@app.route('/pay/confirm/<string:username>', methods=['POST'])
@login_required
def confirm_payment(username: str) -> str:
    """
    Handle confirmation of payment.  This endpoint is triggered when a
    user presses the "Đã nộp tiền" button.  All outstanding amounts for
    the associated student are marked as paid in both the database and
    the original Excel workbook.
    """
    current_user = get_current_user()
    # Determine the target user account.  For admins this is the account
    # identified in the URL; for normal users it must match the current user.
    if current_user.is_admin:
        target_user = User.query.filter_by(username=username).first()
        if not target_user:
            flash('Không tìm thấy tài khoản người dùng.', 'danger')
            return redirect(url_for('summary'))
    else:
        if current_user.username != username:
            flash('Bạn không được phép xác nhận thanh toán cho người khác.', 'danger')
            return redirect(url_for('summary'))
        target_user = current_user
    # Determine which student to apply the payment to.  Admins pass the
    # student name via a hidden form field; normal users use their
    # saved student_name.
    student_name = None
    if current_user.is_admin:
        student_name = request.form.get('student')
        if not student_name:
            flash('Bạn phải chọn học sinh để thanh toán.', 'danger')
            return redirect(url_for('pay', username=username))
    else:
        if not target_user.student_name:
            flash('Tài khoản này chưa chọn tên học sinh.', 'danger')
            return redirect(url_for('pay', username=username))
        student_name = target_user.student_name
    # Mark records for the selected student as paid
    records = ViolationRecord.query.filter_by(student_name=student_name).all()
    any_updated = False
    total_unpaid = 0
    codes_set = set()
    # Compute dynamic due amounts for all records across the database
    all_recs = ViolationRecord.query.all()
    error_defaults = {e.code: e.default_amount for e in ErrorCode.query.all()}
    from collections import defaultdict
    dynamic_due_all: dict[int, int] = {}
    per_key_all: defaultdict[tuple, list] = defaultdict(list)
    for rec_all in all_recs:
        per_key_all[(rec_all.student_name, rec_all.week, rec_all.error_code)].append(rec_all)
    for (stu, wk, code), rec_list in per_key_all.items():
        sorted_list = sorted(rec_list, key=lambda r: (r.date, r.id))
        base_amt = error_defaults.get(code, 0)
        for idx, rrr in enumerate(sorted_list):
            if code in ('VP01', 'VP06'):
                dynamic_due_all[rrr.id] = base_amt + 10000 * idx
            else:
                dynamic_due_all[rrr.id] = base_amt
    for rec in records:
        # compute dynamic due for this record
        dyn_due = dynamic_due_all.get(rec.id, rec.amount_due)
        outstanding = dyn_due - rec.amount_paid
        if outstanding > 0:
            total_unpaid += outstanding
            codes_set.add(rec.error_code)
            # Mark record as fully paid (set amount_paid equal to dynamic due)
            rec.amount_paid = dyn_due
            # Record the payment date in local (GMT+7) timezone
            rec.payment_date = (datetime.utcnow() + timedelta(hours=7)).date()
            any_updated = True
    if any_updated:
        codes_list = sorted(codes_set)
        # Build transfer description
        payment_message = generate_payment_message(student_name, total_unpaid, codes_list)
        # For normal users, attribute payment to the user; for admins,
        # attribute payment to themselves since they initiated the payment.
        payment_user_id = current_user.id
        # Use local time (UTC+7) for the transfer date
        transfer_dt = datetime.utcnow() + timedelta(hours=7)
        payment = Payment(
            user_id=payment_user_id,
            amount=total_unpaid,
            error_code=', '.join(codes_list),
            note=payment_message,
            transfer_date=transfer_dt
        )
        db.session.add(payment)
        db.session.commit()
        # update Excel file to reflect new payment amounts
        excel_path = os.path.join(BASE_DIR, '..', 'Danh Sách Vi Phạm .xlsm')
        try:
            update_excel_payment(student_name, excel_path)
        except Exception:
            pass
        flash('Đã ghi nhận thanh toán và cập nhật dữ liệu.', 'success')
        # Record notifications about the payment.  Always create a note for
        # the user performing the payment so they can see this action in
        # their notification list.  Additionally, create a note for each
        # administrator so that admins know which user made a payment.
        try:
            # Message for the paying user uses "Bạn" to refer to themselves.
            pay_msg_user = (
                f'Bạn đã nộp {format_currency(total_unpaid)} cho {student_name} '
                f'({"; ".join(codes_list) if codes_list else ""}) vào ngày '
                f'{transfer_dt.strftime("%d/%m/%Y %H:%M")}.'
            )
            pay_url = url_for('history')
            new_note_user = Notification(user_id=current_user.id, message=pay_msg_user, url=pay_url)
            db.session.add(new_note_user)
            # Prepare a message for administrators referencing the payer's display name
            user_display = current_user.display_name
            admin_message = (
                f'{user_display} đã nộp {format_currency(total_unpaid)} cho {student_name} '
                f'({"; ".join(codes_list) if codes_list else ""}) vào ngày '
                f'{transfer_dt.strftime("%d/%m/%Y %H:%M")}.'
            )
            # Create a notification for each admin (excluding the payer themselves if they are also admin)
            admin_users = User.query.filter_by(is_admin=True).all()
            for admin_user in admin_users:
                if admin_user.id == current_user.id:
                    continue
                note = Notification(user_id=admin_user.id, message=admin_message, url=pay_url)
                db.session.add(note)
            db.session.commit()
        except Exception:
            pass
    else:
        flash('Không có khoản nợ nào để thanh toán.', 'info')
    return redirect(url_for('pay', username=username, **({'student': student_name} if current_user.is_admin else {})))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile() -> str:
    """
    Allow a logged in user to view and update their profile information.
    Users can change their display name, email, password, avatar and
    personal bio.  To make any changes they must supply their current
    password.  Avatar images are stored in ``static/images/avatars`` and
    referenced from within the site.  Upon successful update the user
    record is committed and a success message is flashed.
    """
    user = get_current_user()
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        if not user.check_password(current_password):
            flash('Mật khẩu hiện tại không đúng.', 'danger')
        else:
            # Collect new values with fallbacks
            new_display_name = request.form.get('display_name', user.display_name).strip() or user.display_name
            new_email = request.form.get('email', user.email).strip() or user.email
            bio = request.form.get('bio', user.bio or '').strip() or None
            new_password = request.form.get('new_password')
            new_password2 = request.form.get('new_password2')
            # Validate email uniqueness if changed
            if new_email != user.email and User.query.filter(User.email == new_email, User.id != user.id).first():
                flash('Email này đã được sử dụng.', 'danger')
            else:
                # update password if provided
                if new_password:
                    if new_password != new_password2:
                        flash('Mật khẩu xác nhận không khớp.', 'danger')
                        return render_template('profile.html', user=user, breadcrumbs=[{'label':'Trang Chủ','url': url_for('index')},{'label':'Hồ Sơ','url': url_for('profile')}])
                    user.set_password(new_password)
                user.display_name = new_display_name
                user.email = new_email
                user.bio = bio
                # handle avatar upload
                avatar_file = request.files.get('avatar')
                if avatar_file and avatar_file.filename:
                    filename = secure_filename(avatar_file.filename)
                    # Only allow certain extensions
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in ['.png', '.jpg', '.jpeg', '.gif']:
                        # Create directory if it does not exist
                        avatar_dir = os.path.join(BASE_DIR, 'static', 'images', 'avatars')
                        os.makedirs(avatar_dir, exist_ok=True)
                        unique_name = f"user{user.id}_{int(datetime.utcnow().timestamp())}{ext}"
                        file_path = os.path.join(avatar_dir, unique_name)
                        avatar_file.save(file_path)
                        user.avatar_path = f"avatars/{unique_name}"
                db.session.commit()
                flash('Cập nhật hồ sơ thành công.', 'success')
    return render_template(
        'profile.html',
        user=user,
        breadcrumbs=[{'label': 'Trang Chủ', 'url': url_for('index')}, {'label': 'Hồ Sơ', 'url': url_for('profile')}]
    )


@app.route('/history')
@login_required
def history() -> str:
    user = get_current_user()
    payments = Payment.query.filter_by(user_id=user.id).order_by(Payment.transfer_date.desc()).all()
    return render_template(
        'history.html', user=user, payments=payments, format_currency=format_currency,
        breadcrumbs=[
            {'label': 'Trang Chủ', 'url': url_for('index')},
            {'label': 'Lịch Sử Nộp Tiền', 'url': url_for('history')}
        ]
    )


# -----------------------------------------------------------------------------
# Notification routes
# -----------------------------------------------------------------------------

@app.route('/notifications')
@login_required
def notifications() -> str:
    """
    Display a list of notifications for the current user.  Notifications
    are ordered newest first.  The page allows users to mark all as
    read.  Clicking on a notification directs the user to the
    associated URL (if provided) and automatically marks the
    notification as read.
    """
    user = get_current_user()
    notes = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).all()
    return render_template(
        'notifications.html', user=user, notifications=notes,
        breadcrumbs=[
            {'label': 'Trang Chủ', 'url': url_for('index')},
            {'label': 'Thông Báo', 'url': url_for('notifications')}
        ]
    )


@app.route('/notification/<int:note_id>')
@login_required
def notification_detail(note_id: int) -> str:
    """
    Mark a single notification as read and redirect the user to the
    associated URL.  If the notification has no URL we redirect back
    to the notifications page.
    """
    user = get_current_user()
    note = Notification.query.filter_by(id=note_id, user_id=user.id).first_or_404()
    if not note.is_read:
        note.is_read = True
        db.session.commit()
    # determine redirect target
    target_url = note.url or url_for('notifications')
    return redirect(target_url)


@app.route('/notifications/read_all', methods=['POST'])
@login_required
def notifications_read_all() -> str:
    """
    Mark all notifications for the current user as read.
    """
    user = get_current_user()
    Notification.query.filter_by(user_id=user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    flash('Đã đánh dấu tất cả thông báo là đã đọc.', 'success')
    return redirect(url_for('notifications'))


@app.route('/violation/add', methods=['GET', 'POST'])
@login_required
def add_violation() -> str:
    """
    Allow logged in users to record a new violation for a student.  The
    form collects the student's name, the date, the error code, an
    optional reason and a payable amount.  The week number is
    computed automatically based on the date and the result is saved
    into the database.  If the targeted student already has an
    associated account, a notification is created so that they are
    informed of the new violation.
    """
    user = get_current_user()
    # Only administrators are allowed to record new violations
    if not user.is_admin:
        flash('Chỉ quản trị viên mới có thể ghi vi phạm.', 'danger')
        return redirect(url_for('summary'))
    names = app.config.get('DS_LOP_NAMES', [])
    codes = ErrorCode.query.all()
    if request.method == 'POST':
        student_name = request.form.get('student')
        date_str = request.form.get('date')
        error_code = request.form.get('error_code')
        reason = request.form.get('reason', '').strip()
        amount_str = request.form.get('amount', '').strip()
        notes = request.form.get('notes', '').strip()
        # basic validation
        if not student_name or not date_str or not error_code:
            flash('Vui lòng điền đầy đủ thông tin bắt buộc.', 'danger')
            return render_template('add_violation.html', user=user, names=names, codes=codes)
        try:
            record_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except Exception:
            flash('Ngày không hợp lệ.', 'danger')
            return render_template('add_violation.html', user=user, names=names, codes=codes)
        # compute custom week number
        from .utils import compute_custom_week
        week = compute_custom_week(record_date)
        # map error code to sheet name
        sheet_map = {
            'VP01': 'NHAT_KI_DI_MUON',
            'VP02': 'NG_LA',
            'VP03': 'DOI_CHO',
            'VP04': 'QUEN_DDHT',
            'VP05': 'NGU_TRONG_GIO',
            'VP06': 'NGHI_HOC',
        }
        sheet_name = sheet_map.get(error_code, '')
        # determine amount due
        amount_due = None
        # If user entered a custom amount, use it (remove separators)
        if amount_str:
            cleaned = amount_str.replace('.', '').replace(',', '')
            if cleaned.isdigit():
                try:
                    amount_due = int(cleaned)
                except Exception:
                    amount_due = None
        # If no amount was provided, compute the default.  For certain
        # violations (VP01 and VP06) we apply an incremental penalty: each
        # subsequent offence in the same week for the same student adds
        # 10,000 VND on top of the base default.  For other codes we use
        # the default amount defined in ErrorCode.
        if amount_due is None:
            code_obj = ErrorCode.query.get(error_code)
            base_amount = code_obj.default_amount if code_obj else 0
            # Apply incremental penalty for tardiness (VP01) and unexcused absence (VP06)
            if error_code in ('VP01', 'VP06'):
                # Count previous occurrences of this code for the student in the same week
                prior_count = ViolationRecord.query.filter_by(
                    student_name=student_name,
                    error_code=error_code,
                    week=week
                ).count()
                # The first offence uses the base amount; each additional offence adds 10k
                amount_due = base_amount + 10000 * prior_count
            else:
                amount_due = base_amount
        # create violation record
        rec = ViolationRecord(
            sheet_name=sheet_name,
            week=week,
            date=record_date,
            student_name=student_name,
            error_code=error_code,
            reason=reason if reason else None,
            amount_due=amount_due,
            amount_paid=0,
            payment_date=None,
            notes=notes if notes else None,
        )
        db.session.add(rec)
        db.session.commit()
        # append to original Excel workbook (best effort)
        try:
            from .utils import append_violation_to_excel
            excel_path = os.path.join(BASE_DIR, '..', 'Danh Sách Vi Phạm .xlsm')
            append_violation_to_excel(rec, excel_path)
        except Exception:
            pass
        # Create a notification for the affected student if they have an account.
        # We first look for a user whose ``student_name`` matches the record.  If
        # none exists we fall back to matching on display_name, because many
        # students choose display names identical to their full name.  This
        # ensures that notifications are delivered even before the student has
        # explicitly linked their account to a record via the payment page.
        target_user = User.query.filter_by(student_name=student_name).first()
        if not target_user:
            target_user = User.query.filter_by(display_name=student_name).first()
        if target_user:
            error_obj = ErrorCode.query.get(error_code)
            error_desc = error_obj.description if error_obj else error_code
            message = (
                f'Bạn vừa bị ghi vi phạm {error_code} - {error_desc} vào ngày '
                f'{record_date.strftime("%d/%m/%Y")}.'
            )
            url_link = url_for('summary', student=student_name)
            note = Notification(user_id=target_user.id, message=message, url=url_link)
            db.session.add(note)
            db.session.commit()
        # Also record a notification for the admin who performed the action so
        # they can see their own recent activities in the notification panel.
        try:
            admin_msg = (
                f'Bạn đã ghi vi phạm {error_code} cho {student_name} ngày '
                f'{record_date.strftime("%d/%m/%Y")}.')
            admin_url = url_for('summary', student=student_name)
            admin_note = Notification(user_id=user.id, message=admin_msg, url=admin_url)
            db.session.add(admin_note)
            db.session.commit()
        except Exception:
            pass
        flash('Đã thêm vi phạm thành công.', 'success')
        return redirect(url_for('summary'))
    from datetime import datetime as dt
    return render_template(
        'add_violation.html', user=user, names=names, codes=codes, datetime=dt,
        breadcrumbs=[
            {'label': 'Trang Chủ', 'url': url_for('index')},
            {'label': 'Ghi Vi Phạm', 'url': url_for('add_violation')}
        ]
    )



@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin() -> str:
    user = get_current_user()
    if not user.is_admin:
        flash('Truy cập bị từ chối. Chỉ quản trị viên mới có thể vào.', 'danger')
        return redirect(url_for('summary'))
    # show all complaints and allow marking as resolved
    if request.method == 'POST':
        # handle resolving complaints
        comp_id = request.form.get('resolve_id')
        if comp_id:
            comp = Complaint.query.get(int(comp_id))
            if comp:
                comp.resolved = True
                db.session.commit()
                flash('Đã đánh dấu khiếu nại là đã giải quyết.', 'success')
    complaints = Complaint.query.order_by(Complaint.submitted_at.desc()).all()
    # Retrieve all payment transactions for admin overview
    payments = Payment.query.order_by(Payment.transfer_date.desc()).all()
    # Compute aggregate metrics for payments
    total_payment_count = len(payments)
    total_payment_amount = sum(p.amount for p in payments)
    return render_template(
        'admin.html', user=user, complaints=complaints, payments=payments,
        total_payment_count=total_payment_count, total_payment_amount=total_payment_amount,
        format_currency=format_currency,
        breadcrumbs=[
            {'label': 'Trang Chủ', 'url': url_for('index')},
            {'label': 'Quản Trị', 'url': url_for('admin')}
        ]
    )


# -----------------------------------------------------------------------------
# Admin user management
# -----------------------------------------------------------------------------

@app.route('/admin/users', methods=['GET', 'POST'])
@login_required
def admin_users() -> str:
    """
    Allow administrators to view and manage user accounts.  Admins can
    promote or demote other users and delete accounts.  The current
    admin cannot change their own role or delete themselves to prevent
    locking out the system.

    Regular admins may view this page.  Only super admins are allowed
    to modify other users (toggle admin role or delete accounts).  This
    ensures that at least one super admin retains full control.
    """
    user = get_current_user()
    # Require at least admin privileges to access the user list
    if not user.is_admin:
        flash('Truy cập bị từ chối. Chỉ quản trị viên mới có thể vào.', 'danger')
        return redirect(url_for('index'))
    # Handle POST actions when a super admin submits changes
    if request.method == 'POST':
        if not user.is_super_admin:
            flash('Chỉ siêu quản trị viên mới có quyền chỉnh sửa người dùng.', 'danger')
            return redirect(url_for('admin_users'))
        action = request.form.get('action')
        target_id = request.form.get('user_id')
        if action and target_id:
            try:
                target_id_int = int(target_id)
            except ValueError:
                target_id_int = None
            target_user = User.query.get(target_id_int) if target_id_int else None
            if target_user and target_user.id != user.id:
                if action == 'toggle_admin':
                    target_user.is_admin = not target_user.is_admin
                    db.session.commit()
                    flash(f'Đã cập nhật quyền quản trị cho {target_user.display_name}.', 'success')
                elif action == 'delete':
                    # Remove related objects before deleting
                    Payment.query.filter_by(user_id=target_user.id).delete()
                    Complaint.query.filter_by(user_id=target_user.id).delete()
                    Notification.query.filter_by(user_id=target_user.id).delete()
                    db.session.delete(target_user)
                    db.session.commit()
                    flash(f'Đã xóa tài khoản {target_user.display_name}.', 'success')
            else:
                flash('Không thể thực hiện hành động trên tài khoản này.', 'warning')
    # Retrieve all users for display
    users = User.query.order_by(User.registered_at.desc()).all()
    return render_template(
        'admin_users.html', user=user, users=users,
        breadcrumbs=[
            {'label': 'Trang Chủ', 'url': url_for('index')},
            {'label': 'Quản Trị', 'url': url_for('admin')},
            {'label': 'Quản Lí User', 'url': url_for('admin_users')}
        ]
    )


@app.route('/admin/delete_violation/<int:violation_id>', methods=['POST'])
@login_required
def delete_violation(violation_id: int) -> str:
    """
    Allow an administrator to delete a specific violation record.  This
    endpoint is triggered from the admin complaints view and will
    remove the record from the database and attempt to remove it from
    the original Excel workbook.  Upon completion the admin is
    returned to the admin page with a flash message.
    """
    user = get_current_user()
    if not user.is_admin:
        flash('Truy cập bị từ chối.', 'danger')
        return redirect(url_for('index'))
    record = ViolationRecord.query.get_or_404(violation_id)
    # Remove from Excel
    excel_path = os.path.join(BASE_DIR, '..', 'Danh Sách Vi Phạm .xlsm')
    try:
        remove_violation_from_excel(record, excel_path)
    except Exception:
        pass
    # Delete the record and any associated complaints
    Complaint.query.filter_by(violation_id=record.id).delete()
    db.session.delete(record)
    db.session.commit()
    flash('Đã xóa vi phạm thành công.', 'success')
    return redirect(url_for('admin'))

# -----------------------------------------------------------------------------
# Utility API endpoints
# -----------------------------------------------------------------------------

@app.route('/violation/calculate_amount')
def calculate_amount_api() -> 'Response':
    """
    Compute the amount due for a prospective violation based on the
    student name, date and error code.  This endpoint is used by
    the front-end to auto-populate the 'Số tiền phải nộp' field in
    the add violation form.  It takes query parameters:
      - student: the student's full name (string)
      - date: the violation date in yyyy-mm-dd format
      - error_code: the violation code (e.g. VP01)
    It returns a JSON object {"amount": int} with the calculated
    penalty.  If missing or invalid parameters are provided, the
    amount defaults to 0.
    """
    student = request.args.get('student')
    date_str = request.args.get('date')
    code = request.args.get('error_code')
    amount = 0
    try:
        if student and date_str and code:
            # parse date
            record_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            from .utils import compute_custom_week
            week = compute_custom_week(record_date)
            # base default amount
            code_obj = ErrorCode.query.get(code)
            base_amount = code_obj.default_amount if code_obj else 0
            if code in ('VP01', 'VP06'):
                # count previous occurrences for incremental penalty
                prior_count = ViolationRecord.query.filter_by(
                    student_name=student,
                    error_code=code,
                    week=week
                ).count()
                amount = base_amount + 10000 * prior_count
            else:
                amount = base_amount
    except Exception:
        amount = 0
    return jsonify({'amount': amount})


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

with app.app_context():
    initialise_database()
    
if __name__ == '__main__':
    # When running directly, perform a quick environment check.  The web app
    # should be reachable at http://localhost:5000
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
