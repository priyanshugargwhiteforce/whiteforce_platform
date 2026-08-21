import os
import socket
import platform
import logging.config
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Core ─────────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-change-me-in-production')
DEBUG = os.environ.get('DJANGO_DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost,*').split(',')

# ── API Key (whiteforce notifications) ───────────────────────────────────────
# Clients must send:  X-API-KEY: <value>  header on every notification request.
API_SECRET_KEY = os.environ.get('API_SECRET_KEY', '')

# ── Installed Apps ────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'rest_framework',

    # ── Tally API apps ────────────────────────────────────────────────────
    'config',       # tally API key/config model
    'tallyapp',     # tally XML integration
    'corsheaders',

    # ── Whiteforce Notifications app ──────────────────────────────────────
    'notifications',
    # --validator email
    'validator',

    #---Bulk Resume Upload
    'bulkresume',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

CORS_ALLOW_ALL_ORIGINS = True
ROOT_URLCONF = 'core.urls'
WSGI_APPLICATION = 'core.wsgi.application'
ASGI_APPLICATION = 'core.asgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# ── Database ──────────────────────────────────────────────────────────────────
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME'),
        'USER': os.environ.get('DB_USER'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'Whiteforce123@'),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}
# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.sqlite3',
#         'NAME': BASE_DIR / 'db.sqlite3',
#     }
# }

# ── Cache (used by notifications rate limiter) ────────────────────────────────
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'whiteforce-ratelimit',
    }
}



# ── DRF ───────────────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    # Notifications use API key auth; tally views are open (or add your own)
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'notifications.authentication.ApiKeyAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',  # tally is open by default
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'notifications.throttles.CreateNotificationThrottle',
        'notifications.throttles.SendNotificationThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'create_notification': '30/minute',
        'send_notification':   '20/minute',
        'validate_tokens':     '15/minute',
        'send_wira': '30/minute',
        'bulk_resume_upload': '10/minute',
        
    },
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.MultiPartParser',
        'rest_framework.parsers.FormParser',
        'rest_framework.parsers.JSONParser',
    ],
}

# ── Auth password validators ───────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ── i18n ──────────────────────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ── Static / Media ────────────────────────────────────────────────────────────
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── FCM credentials (whiteforce notifications) ────────────────────────────────
FCM_CREDENTIALS = {
    'jobs': os.environ.get(
        'FCM_CRED_JOBS',
        str(BASE_DIR / 'credentials' / 'white-force-jobs-service-account.json'),
    ),
    'attendance': os.environ.get(
        'FCM_CRED_ATTENDANCE',
        str(BASE_DIR / 'credentials' / 'white-force-attendance-service-account.json'),
    ),
}

# ── Logging ───────────────────────────────────────────────────────────────────
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name} | {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'platform.log',
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'loggers': {
        'notifications': {
            'handlers': ['file', 'console'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
        'tallyapp': {
            'handlers': ['file', 'console'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
        'bulkresume': {
            'handlers': ['file', 'console'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'root': {
            'handlers': ['console'],
            'level': 'WARNING',
        },
    },
}

# ── Redis ────────────────────────────────────────────────────────────────────
REDIS_CONFIG = {
    'HOST':     '127.0.0.1',
    'PORT':     6379,
    'DB':       0,
    'PASSWORD': None,
}

# ── Celery ────────────────────────────────────────────────────────────────────
CELERY_BROKER_URL = f"redis://{REDIS_CONFIG['HOST']}:{REDIS_CONFIG['PORT']}/{REDIS_CONFIG['DB']}"
CELERY_RESULT_BACKEND = f"redis://{REDIS_CONFIG['HOST']}:{REDIS_CONFIG['PORT']}/{REDIS_CONFIG['DB']}"
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ROUTES = {
    'bulkresume.tasks.process_resume_task': {'queue': 'resume_parsing'},
}

# ── Celery resilience (WSL mirrored networking connection-drop mitigation) ────
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_MAX_RETRIES = None  # infinite retries, never give up
CELERY_BROKER_CONNECTION_TIMEOUT = 30

# Build keepalive options using real socket constants (fixes Error 22 on Linux —
# previously hardcoded ints 1/2/3 didn't match TCP_KEEPIDLE/INTVL/CNT on this OS).
# hasattr guard keeps this safe if it ever runs on macOS/Windows, where some of
# these constants don't exist.
_socket_keepalive_options = {}
if hasattr(socket, 'TCP_KEEPIDLE'):
    _socket_keepalive_options[socket.TCP_KEEPIDLE] = 60   # seconds before first probe
if hasattr(socket, 'TCP_KEEPINTVL'):
    _socket_keepalive_options[socket.TCP_KEEPINTVL] = 10  # interval between probes
if hasattr(socket, 'TCP_KEEPCNT'):
    _socket_keepalive_options[socket.TCP_KEEPCNT] = 5     # failed probes before dead

CELERY_BROKER_TRANSPORT_OPTIONS = {
    'socket_keepalive': True,
    'socket_keepalive_options': _socket_keepalive_options,
    'retry_on_timeout': True,
}

# Worker-level: agar connection lost ho, currently running task cancel na ho
worker_cancel_long_running_tasks_on_connection_loss = False

# ── Groq — multiple keys (temporary, until company upgrades to paid tier) ────
# settings.py mein
GROQ_API_KEYS = [
    v for k, v in sorted(os.environ.items())
    if k.startswith('GROQ_API_KEY_') and v
]
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'openai/gpt-oss-20b')

# ── BulkResume: OS-specific tool paths (Tesseract / Poppler / LibreOffice) ────
if platform.system() == 'Windows':
    TESSERACT_CMD = os.environ.get(
        'TESSERACT_CMD', r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    )
    POPPLER_PATH = os.environ.get(
        'POPPLER_PATH', r'C:\poppler-26.02.0\Library\bin'
    )
    SOFFICE_PATH = os.environ.get(
        'SOFFICE_PATH', r'C:\Program Files\LibreOffice\program\soffice.exe'
    )
else:
    # Linux (VPS) — tools resolved from system PATH after `apt install`
    TESSERACT_CMD = os.environ.get('TESSERACT_CMD', None)
    POPPLER_PATH = os.environ.get('POPPLER_PATH', None)
    SOFFICE_PATH = os.environ.get('SOFFICE_PATH', 'soffice')

# ── Email Validator ───────────────────────────────────────────────────────────
EMAIL_VALIDATOR = {
    'MAX_WORKERS':      50,
    'BATCH_MAX_EMAILS': 500,
}