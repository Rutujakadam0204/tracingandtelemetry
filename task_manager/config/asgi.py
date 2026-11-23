import os

from django.core.asgi import get_asgi_application

from config.telemetry import attach_log_handler, instrument_django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Before get_asgi_application(): see config/wsgi.py.
instrument_django()

application = get_asgi_application()

# After get_asgi_application(): see config/wsgi.py.
attach_log_handler()
