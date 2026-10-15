"""The app server of cc-observatory: hook ingestion, state, ``/metrics`` and the web UI.

Server-only: its modules import the ``server`` extra (FastAPI, SQLAlchemy, pydantic and
friends), which ``uvx cc-observatory`` does not install. The CLI and ``ccobs.domain``
never import this package (ADR 0008; ``tests/test_cli_light.py`` checks it).
"""
