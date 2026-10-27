"""ENAS HAJI booking backend: FastAPI + SQLite + admin panel + optional SMTP alerts."""
from __future__ import annotations

import base64
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from email.message import EmailMessage
import csv
import hashlib
import hmac
import io
import logging
import os
from pathlib import Path
import re
import secrets
import smtplib
import sqlite3
import threading
import time
import unicodedata
from typing import Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
DATA_DIR = ROOT / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = Path(os.getenv('DB_PATH', str(DATA_DIR / 'bookings.sqlite3')))
SECRET = os.getenv('SECRET_KEY', '')
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '').strip().lower()
ADMIN_PASSWORD_HASH = os.getenv('ADMIN_PASSWORD_HASH', '')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
logger = logging.getLogger('enas-haji')

# Reject accidental public deployments without configured secrets.
if len(SECRET) < 32 or not ADMIN_EMAIL or not ADMIN_PASSWORD_HASH:
    raise RuntimeError('Run python setup.py first. A secure .env file is required.')

SERVICES = (
    'تصميم توب حسب الطلب', 'مكياج عروس', 'مكياج سهرات',
    'مكياج مناسبات', 'الاستفسار عن توب محدد',
)
STATUSES = ('new', 'contacted', 'confirmed', 'completed', 'cancelled')
STATUS_AR = {'new': 'جديد', 'contacted': 'قيد المتابعة', 'confirmed': 'مؤكد',
             'completed': 'مكتمل', 'cancelled': 'ملغي'}


def db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    with db_connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS bookings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL,
              preferred_date TEXT, notes TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'new',
              internal_note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_booking_status ON bookings(status);
            CREATE INDEX IF NOT EXISTS idx_booking_created ON bookings(created_at);
        ''')


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title='ENAS HAJI – Bookings', docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=12 * 60 * 60,
                   same_site='strict', https_only=COOKIE_SECURE, session_cookie='enas_admin')


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/admin') or request.url.path.startswith('/api/admin') else 'no-cache'
    return response


def password_valid(plain: str, stored: str) -> bool:
    try:
        method, iterations, salt64, digest64 = stored.split('$')
        if method != 'pbkdf2_sha256': return False
        expected = base64.urlsafe_b64decode(digest64)
        digest = hashlib.pbkdf2_hmac('sha256', plain.encode(), base64.urlsafe_b64decode(salt64), int(iterations))
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError, base64.binascii.Error):
        return False


class BookingIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    phone: str = Field(min_length=7, max_length=24)
    service: str
    preferred_date: date | None = None
    notes: str = Field(default='', max_length=500)
    website: str = Field(default='', max_length=100)  # honeypot

    @field_validator('name')
    @classmethod
    def clean_name(cls, value: str):
        value = ' '.join(value.split())
        if len(value) < 2: raise ValueError('يرجى إدخال الاسم')
        return value

    @field_validator('phone')
    @classmethod
    def check_phone(cls, value: str):
        value = value.strip()
        digits = ''.join(str(unicodedata.digit(c)) for c in value if c.isdigit())
        if not 9 <= len(digits) <= 15: raise ValueError('رقم الهاتف غير صحيح')
        if not all(c.isdigit() or c in '+-() .\u066c\u066b' for c in value):
            raise ValueError('رقم الهاتف يحتوي على رموز غير صالحة')
        return value

    @field_validator('service')
    @classmethod
    def check_service(cls, value):
        if value not in SERVICES: raise ValueError('الخدمة غير مدعومة')
        return value


# In-memory protection suitable for a single small-server instance.
rate_lock = threading.Lock()
request_times = defaultdict(list)


def throttle(ip: str, max_calls=6, window_s=600):
    now = time.monotonic()
    with rate_lock:
        calls = [t for t in request_times[ip] if t > now - window_s]
        if len(calls) >= max_calls:
            raise HTTPException(status_code=429, detail='طلبات كثيرة، يرجى المحاولة لاحقًا')
        calls.append(now)
        request_times[ip] = calls
        if len(request_times) > 10000:
            for k in list(request_times)[:1000]:
                if request_times[k][-1] < now - window_s:
                    request_times.pop(k, None)


def notify_by_email(data: dict):
    """Notifications are optional. A failed email never loses the saved booking."""
    host = os.getenv('SMTP_HOST', '').strip()
    dest = os.getenv('NOTIFY_EMAIL', '').strip()
    sender = os.getenv('SMTP_FROM', '').strip()
    if not (host and dest and sender): return
    try:
        msg = EmailMessage()
        msg['Subject'] = f"طلب جديد من ENAS HAJI — {data['reference']}"
        msg['From'] = sender
        msg['To'] = dest
        msg.set_content(f"""طلب جديد من الموقع
الرقم: {data['reference']}
الاسم: {data['name']}
الهاتف: {data['phone']}
الخدمة: {data['service']}
التاريخ المفضل: {data['preferred_date'] or 'غير محدد'}
ملاحظات: {data['notes'] or 'لا يوجد'}

لوحة التحكم: {os.getenv('PUBLIC_SITE_URL', '').rstrip('/')}/admin
""")
        port = int(os.getenv('SMTP_PORT', '587'))
        user, password = os.getenv('SMTP_USERNAME', ''), os.getenv('SMTP_PASSWORD', '')
        if os.getenv('SMTP_USE_SSL', 'false').lower() == 'true':
            client = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            client = smtplib.SMTP(host, port, timeout=15)
        with client as smtp:
            if os.getenv('SMTP_USE_STARTTLS', 'true').lower() == 'true' and not os.getenv('SMTP_USE_SSL', 'false').lower() == 'true':
                smtp.starttls()
            if user: smtp.login(user, password)
            smtp.send_message(msg)
    except Exception:
        logger.exception('Could not send booking email notification')


@app.get('/', include_in_schema=False)
async def home():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.post('/api/bookings', status_code=201)
async def create_booking(booking: BookingIn, request: Request, tasks: BackgroundTasks):
    if booking.website:  # Ignore scripted bot submissions without revealing filter.
        return {'ok': True, 'reference': 'EH-RECEIVED'}
    ip = request.client.host if request.client else 'unknown'
    throttle(ip)
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with db_connect() as db:
        cur = db.execute('''
            INSERT INTO bookings (name,phone,service,preferred_date,notes,status,created_at,updated_at)
            VALUES (?, ?, ?, ?, ?, 'new', ?, ?)
        ''', (booking.name, booking.phone, booking.service,
              booking.preferred_date.isoformat() if booking.preferred_date else None,
              booking.notes, now, now))
        ident = cur.lastrowid
    reference = f'EH-{ident:06d}'
    tasks.add_task(notify_by_email, {'reference': reference, 'name': booking.name, 'phone': booking.phone,
                                    'service': booking.service, 'preferred_date': str(booking.preferred_date or ''), 'notes': booking.notes})
    return {'ok': True, 'reference': reference,
            'message': 'تم تسجيل طلبك. سيتواصل فريق ENAS HAJI معك لتأكيد التفاصيل.'}


@app.get('/health')
async def health():
    return {'ok': True}


@app.get('/admin/login', response_class=HTMLResponse, include_in_schema=False)
async def admin_login_page(request: Request):
    if request.session.get('admin'):
        return RedirectResponse('/admin', status_code=303)
    return FileResponse(ROOT / 'static' / 'login.html')


@app.post('/admin/login', include_in_schema=False)
async def admin_login(request: Request, email: str = Form(''), password: str = Form('')):
    ip = request.client.host if request.client else 'unknown'
    throttle('login:' + ip, max_calls=10, window_s=600)
    if not hmac.compare_digest(email.strip().lower(), ADMIN_EMAIL) or not password_valid(password, ADMIN_PASSWORD_HASH):
        return RedirectResponse('/admin/login?error=1', status_code=303)
    request.session.clear()
    request.session['admin'] = True
    request.session['csrf'] = secrets.token_urlsafe(32)
    return RedirectResponse('/admin', status_code=303)


async def is_admin(request: Request):
    if request.session.get('admin') is not True:
        raise HTTPException(status_code=401, detail='يجب تسجيل الدخول')


def require_csrf(request: Request):
    expected = request.session.get('csrf', '')
    given = request.headers.get('X-CSRF-Token', '')
    if not expected or not hmac.compare_digest(expected, given):
        raise HTTPException(status_code=403, detail='رمز الحماية غير صحيح')


@app.get('/admin', include_in_schema=False)
async def admin_page(request: Request):
    if not request.session.get('admin'):
        return RedirectResponse('/admin/login', status_code=303)
    admin_html = (ROOT / 'static' / 'admin.html').read_text(encoding='utf-8')
    # Token is URL-safe only and cannot inject markup.
    admin_html = admin_html.replace('__CSRF_TOKEN__', request.session['csrf'])
    return HTMLResponse(admin_html)


@app.post('/api/admin/logout', dependencies=[Depends(is_admin)])
async def logout(request: Request):
    require_csrf(request)
    request.session.clear()
    return {'ok': True}


@app.get('/api/admin/bookings', dependencies=[Depends(is_admin)])
async def get_bookings(q: str = '', status: str = '', page: int = 1):
    if status and status not in STATUSES: raise HTTPException(status_code=400, detail='حالة غير معروفة')
    page = min(max(page, 1), 10000)
    q = q.strip()[:80]
    parts, args = [], []
    if status:
        parts.append('status = ?')
        args.append(status)
    if q:
        parts.append('(name LIKE ? OR phone LIKE ? OR service LIKE ? OR notes LIKE ?)')
        args.extend(['%' + q + '%'] * 4)
    where = ' WHERE ' + ' AND '.join(parts) if parts else ''
    with db_connect() as db:
        total = db.execute('SELECT COUNT(*) FROM bookings' + where, args).fetchone()[0]
        records = db.execute('SELECT * FROM bookings' + where + ' ORDER BY id DESC LIMIT 50 OFFSET ?',
                             args + [(page - 1) * 50]).fetchall()
    return {'total': total, 'page': page, 'pages': max(1, (total + 49) // 50), 'items': [dict(x) for x in records]}


@app.get('/api/admin/stats', dependencies=[Depends(is_admin)])
async def get_stats():
    with db_connect() as db:
        counts = {r['status']: r['count'] for r in db.execute('SELECT status, COUNT(*) count FROM bookings GROUP BY status')}
        total = db.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]
        today = datetime.now(timezone.utc).date().isoformat()
        today_count = db.execute('SELECT COUNT(*) FROM bookings WHERE substr(created_at, 1, 10) = ?', (today,)).fetchone()[0]
    return {'total': total, 'today': today_count, 'statuses': {s: counts.get(s, 0) for s in STATUSES}}


class BookingUpdate(BaseModel):
    status: Literal['new', 'contacted', 'confirmed', 'completed', 'cancelled']
    internal_note: str = Field(default='', max_length=300)


@app.patch('/api/admin/bookings/{booking_id}', dependencies=[Depends(is_admin)])
async def update_booking(booking_id: int, update: BookingUpdate, request: Request):
    require_csrf(request)
    with db_connect() as db:
        cur = db.execute('''UPDATE bookings SET status=?, internal_note=?, updated_at=? WHERE id=?''',
                         (update.status, update.internal_note,
                          datetime.now(timezone.utc).isoformat(timespec='seconds'), booking_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail='الطلب غير موجود')
    return {'ok': True}


@app.get('/api/admin/export', dependencies=[Depends(is_admin)])
async def export_bookings():
    """Admin-only CSV. Avoid spreadsheet formula injection for user-entered fields."""
    with db_connect() as db:
        records = [dict(r) for r in db.execute('SELECT * FROM bookings ORDER BY id DESC')]
    output = io.StringIO()
    writer = csv.writer(output)
    columns = ['id','name','phone','service','preferred_date','notes','status','internal_note','created_at']
    writer.writerow(['الرقم','الاسم','الهاتف','الخدمة','التاريخ المفضل','ملاحظات العميلة','الحالة','ملاحظات الإدارة','تاريخ الطلب'])
    for item in records:
        def safe_csv(v):
            v = '' if v is None else str(v)
            return "'" + v if v and v.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else v
        writer.writerow([safe_csv(item.get(k)) for k in columns])
    output.seek(0)
    return StreamingResponse(iter([output.getvalue().encode('utf-8-sig')]), media_type='text/csv; charset=utf-8',
                             headers={'Content-Disposition': 'attachment; filename="enas-haji-bookings.csv"'})
