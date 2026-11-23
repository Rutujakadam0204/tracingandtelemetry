import os

from django.core.wsgi import get_wsgi_application

from config.telemetry import attach_log_handler, instrument_django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Before get_wsgi_application(): the OTel middleware must be in place when Django
# builds its middleware chain. `runserver` imports this module too (WSGI_APPLICATION).
instrument_django()

application = get_wsgi_application()

# After get_wsgi_application(): it calls django.setup(), which re-applies
# settings.LOGGING and would remove a handler attached any earlier.
attach_log_handler()
