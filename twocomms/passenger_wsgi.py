import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

# Shared-host Passenger does not execute manage.py, so load the same private
# environment file before Django imports settings. Existing cPanel variables
# remain authoritative because python-dotenv defaults to override=False.
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
if load_dotenv:
    base_dir = Path(__file__).resolve().parent
    for candidate in (base_dir / '.env.production', base_dir.parent / '.env.production'):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            break

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twocomms.production_settings")
from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
