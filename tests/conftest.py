"""Pytest fixtures: temporary test client and role helpers."""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from app.bootstrap import DEMO_TOKENS


@pytest.fixture(scope="session")
def test_app() -> Iterator[Flask]:
    data_dir = tempfile.mkdtemp(prefix="runbook-pytest-")
    old_env = os.environ.get("RUNBOOK_DATA_DIR")
    os.environ["RUNBOOK_DATA_DIR"] = data_dir
    os.environ["RUNBOOK_SEED_ON_BOOT"] = "1"
    os.environ["RUNBOOK_DEMO_SESSIONS"] = "on"
    try:
        app = create_app()
        app.config["TESTING"] = True
        yield app
    finally:
        if old_env is not None:
            os.environ["RUNBOOK_DATA_DIR"] = old_env
        else:
            os.environ.pop("RUNBOOK_DATA_DIR", None)
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
def client(test_app: Flask) -> FlaskClient:
    return test_app.test_client()


class RoleClients:
    def __init__(self, app: Flask) -> None:
        self.app = app

    def for_role(self, role: str) -> FlaskClient:
        c = self.app.test_client()
        token = DEMO_TOKENS.get(role)
        if token:
            c.set_cookie("runbook_session", token)
        return c

    @property
    def anon(self) -> FlaskClient:
        return self.app.test_client()

    @property
    def organizer(self) -> FlaskClient:
        return self.for_role("organizer")

    @property
    def admin(self) -> FlaskClient:
        return self.for_role("admin")

    @property
    def judge_a(self) -> FlaskClient:
        return self.for_role("judge_a")

    @property
    def judge_b(self) -> FlaskClient:
        return self.for_role("judge_b")

    @property
    def participant(self) -> FlaskClient:
        return self.for_role("participant")


@pytest.fixture
def roles(test_app: Flask) -> RoleClients:
    return RoleClients(test_app)
