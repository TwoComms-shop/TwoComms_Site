"""Disposable InnoDB gate for Instagram's real migration graph.

The scoped runner applies management, sessions, sites, accounts and product catalog
leaves with their real dependencies, plus the storefront predecessor required
by Instagram. Unrelated leaf migrations are outside this proof. Credential,
loopback, network and disposable-database fences come from the parent profile.
"""

from test_settings_mariadb import *  # noqa: F401,F403


TEST_MARIADB_SCOPE = "instagram-real-migration-dependencies"
