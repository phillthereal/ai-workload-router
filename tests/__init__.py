"""
Tests for the AI Workload Router.

Force every adapter/judge call in the test suite onto the offline mock path
— this MUST be set before any test module imports router.adapters or
router.scoring, so tests never hit real provider APIs even though .env may
have real keys configured (see router.secrets.force_mock and
router.adapters.get_adapter). unittest discover imports this package
__init__ before importing the individual test_*.py modules within it, so
setting the env var here is early enough.
"""

import os

os.environ.setdefault("AWR_FORCE_MOCK", "1")
