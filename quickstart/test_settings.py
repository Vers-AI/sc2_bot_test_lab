"""Settings for running the test_lab test suite without MySQL or docker.

The regular lab configuration stores the test_lab models in MySQL (see
``quickstart/settings.py`` and the router in ``quickstart/db_router.py``).
The unit tests only need a relational database, so this module points every
alias at a local SQLite file and drops the router.

Run the tests with::

    python quickstart/manage.py test test_lab --settings=test_lab.quickstart.test_settings
"""

from .settings import *  # noqa: F401,F403
from .settings import BASE_DIR

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db_test.sqlite3',
    },
}

# Every model (including the test_lab ones) lives in the 'default' database.
DATABASE_ROUTERS = []
