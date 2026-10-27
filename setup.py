"""Interactive first-time setup. Keeps credentials out of distributable project files."""
from pathlib import Path
import getpass
import hashlib
import secrets
import base64

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / '.env'

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 260000)
    return 'pbkdf2_sha256$260000$' + base64.urlsafe_b64encode(salt).decode() + '$' + base64.urlsafe_b64encode(digest).decode()

def escape_env(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '') + '"'

def main():
    if ENV_FILE.exists():
        print('ملف .env موجود بالفعل. تم إيقاف الإعداد منعًا للكتابة فوق بياناتك.')
        return
    print('إعداد لوحة تحكم ENAS HAJI لأول مرة')
    while True:
        email = input('البريد الإلكتروني للإدارة: ').strip().lower()
        if '@' in email and '.' in email.rsplit('@', 1)[-1]: break
        print('اكتب بريدًا إلكترونيًا صالحًا.')
    while True:
        password = getpass.getpass('كلمة مرور قوية (12 حرفًا على الأقل): ')
        confirmation = getpass.getpass('إعادة كلمة المرور: ')
        if len(password) >= 12 and password == confirmation: break
        print('كلمة المرور يجب أن تكون 12 حرفًا على الأقل ومتطابقة.')
    lines = {
        'SECRET_KEY': secrets.token_urlsafe(48),
        'ADMIN_EMAIL': email,
        'ADMIN_PASSWORD_HASH': hash_password(password),
        'COOKIE_SECURE': 'false',
        'SMTP_HOST': '', 'SMTP_PORT': '587',
        'SMTP_USERNAME': '', 'SMTP_PASSWORD': '',
        'SMTP_FROM': '', 'SMTP_USE_SSL': 'false', 'SMTP_USE_STARTTLS': 'true',
        'NOTIFY_EMAIL': email,
        'PUBLIC_SITE_URL': 'http://127.0.0.1:8000',
    }
    ENV_FILE.write_text(''.join(k + '=' + escape_env(v) + '\n' for k, v in lines.items()), encoding='utf-8')
    print('\nتم إنشاء .env بنجاح. شغّل: uvicorn app:app --reload')
    print('الموقع: http://127.0.0.1:8000  |  لوحة التحكم: http://127.0.0.1:8000/admin')
    print('تنبيه: الإيميل لن يرسل حتى تضبط بيانات SMTP الحقيقية في .env')

if __name__ == '__main__': main()
