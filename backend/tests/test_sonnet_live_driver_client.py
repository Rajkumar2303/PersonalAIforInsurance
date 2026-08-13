"""Focused tests for the backend-client Sonnet live driver.

Covers (hermetically, no live route):
- the supplied intake_session_id is sent to the backend browser-session API
- no local/pilot profile is constructed
- missing session / declined consent stops safely
- normal finite pause -> approve -> resume uses the same session + attempt
- a repeated IDENTICAL pause stops safely with stalled_no_progress
- EOF / user quit stop and close the browser
- quote detection calls the normalized-quote endpoint and terminates
"""

from __future__ import annotations

import argparse

import pytest

from demos import sonnet_live_driver as driver
from demos.sonnet_live_driver import BackendError


class FakeClient:
    def __init__(self, script: dict, raises: set[str] | None = None) -> None:
        self.script = script
        self.raises = raises or set()
        self.calls: list[tuple] = []

    def _respond(self, path: str):
        if any(path.startswith(r) for r in self.raises):
            raise BackendError("not found")
        for key in sorted(self.script, key=len, reverse=True):
            if path.startswith(key):
                return self.script[key]
        raise RuntimeError(f"unexpected request {path}")

    def get(self, path):
        self.calls.append(("GET", path))
        return self._respond(path)

    def post(self, path, body=None):
        self.calls.append(("POST", path, body or {}))
        return self._respond(path)

    def delete(self, path):
        self.calls.append(("DELETE", path))
        return {}

    def paths(self):
        return [c[1] for c in self.calls]


PLAN_READY = {
    "routes": [
        {"registry_id": "sonnet", "is_ready": True, "blockers": [], "channels": [{"kind": "online"}]}
    ]
}
SESSION = {"session_id": "intake-abc", "status": "active", "profile_id": "prof-1"}


def _run_state(status: str, obs: dict) -> dict:
    return {
        "session": {"browser_session_id": "bs-1", "attempt_id": "att-1", "status": status},
        "step": {"status": status, "observation": obs},
    }


RUN_PAUSE = _run_state("paused_human_checkpoint", {
    "checkpoint": {"checkpoint_type": "identity_lookup", "must_not_automate": False},
})
RUN_QUOTE = _run_state("succeeded", {"quote": {"quote_present": True}})
QUOTE = {
    "normalized_quote_id": "nq-1", "registry_id": "sonnet", "attempt_id": "att-1",
    "normalized_at": "2026-08-12T00:00:00Z",
    "premium": {"normalized_annual_amount": "1200", "currency": "CAD", "provider_presented_frequency": "annual"},
    "coverage_ledger": {"items": [{"item_key": "third_party_liability", "state": "known"}]},
}


def _args(session_id: str = "intake-abc", max_steps: int = 40) -> argparse.Namespace:
    return argparse.Namespace(intake_session_id=session_id, base_url="http://127.0.0.1:8000",
                              registry_id="sonnet", max_steps=max_steps)


def _script(session=SESSION, plan=PLAN_READY, run=RUN_PAUSE, resume=RUN_QUOTE, quote=QUOTE):
    return {
        "/api/v1/planner/plan": plan,
        f"/api/v1/intake/sessions/{session['session_id']}/consent/route": {},
        f"/api/v1/intake/sessions/{session['session_id']}/consent": {},
        f"/api/v1/intake/sessions/{session['session_id']}/route-disclosure": {"path_details": []},
        f"/api/v1/intake/sessions/{session['session_id']}": session,
        "/api/v1/browser/sessions/bs-1/approve-checkpoint": {},
        "/api/v1/browser/sessions/bs-1/quote": quote,
        "/api/v1/browser/sessions/bs-1/resume": resume,
        "/api/v1/browser/sessions/bs-1/run": run,
        "/api/v1/browser/sessions/bs-1": {"session": {"browser_session_id": "bs-1", "attempt_id": "att-1"}, "started": True},
        "/api/v1/browser/sessions": {"started": True, "session": {"browser_session_id": "bs-1", "attempt_id": "att-1"}},
    }


@pytest.fixture(autouse=True)
def _yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")


async def test_driver_sends_intake_session_and_no_pilot(monkeypatch) -> None:
    client = FakeClient(_script())
    rc = await driver.run(client, _args())
    assert rc == 0
    create = next(c for c in client.calls if c[0] == "POST" and c[1] == "/api/v1/browser/sessions")
    body = create[2]
    assert body["intake_session_id"] == "intake-abc"
    assert body["planned_route_id"] == "sonnet"
    assert body["execution_mode"] == "live"
    assert body["live_gate"]["personal_use_confirmed"] is True
    assert body["live_gate"]["accurate_information_attested"] is True
    assert not hasattr(driver, "_build_pilot_engine")
    assert not hasattr(driver, "BrowserManager")
    assert not hasattr(driver, "IntakeEngine")
    assert "profile" not in body


async def test_driver_missing_session_stops_safely() -> None:
    client = FakeClient(_script(), raises={"/api/v1/intake/sessions/"})
    rc = await driver.run(client, _args())
    assert rc == 2
    assert all("/api/v1/browser/sessions" not in p for p in client.paths())


async def test_driver_consent_declined_stops_before_browser(monkeypatch) -> None:
    # "no" is NOT yes -> _ask_yes returns False immediately (no loop).
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    client = FakeClient(_script())
    rc = await driver.run(client, _args())
    assert rc == 3
    assert all("/api/v1/browser/sessions" not in p for p in client.paths())


async def test_driver_normal_finite_pause_approve_resume(monkeypatch) -> None:
    """Normal finite sequence: start -> pause -> approve/resume -> quote."""
    client = FakeClient(_script())
    rc = await driver.run(client, _args())
    assert rc == 0
    posts = [c for c in client.calls if c[0] == "POST"]
    approve = next(c for c in posts if c[1].endswith("/approve-checkpoint"))
    resumes = [c for c in posts if c[1].endswith("/resume")]
    assert "bs-1" in approve[1]
    assert all("bs-1" in p[1] for p in resumes)
    assert approve[2] == {"checkpoint_type": "identity_lookup"}
    assert any(c[0] == "DELETE" and c[1] == "/api/v1/browser/sessions/bs-1" for c in client.calls)


async def test_driver_repeated_identical_pause_stalls(monkeypatch) -> None:
    """A repeated IDENTICAL paused state must stop safely (no infinite YES)."""
    # run AND resume both return the same identity_lookup pause.
    client = FakeClient(_script(run=RUN_PAUSE, resume=RUN_PAUSE))
    rc = await driver.run(client, _args())
    assert rc == 7  # stalled_no_progress
    approves = [c for c in client.calls if c[0] == "POST" and c[1].endswith("/approve-checkpoint")]
    # At most two approvals before the stall guard stops the loop.
    assert len(approves) == 2
    # Browser was closed.
    assert any(c[0] == "DELETE" and c[1] == "/api/v1/browser/sessions/bs-1" for c in client.calls)


async def test_driver_eof_stops_and_closes_browser(monkeypatch) -> None:
    """EOF at the identity prompt must stop and close the browser."""
    calls = {"n": 0}

    def seq_input(prompt=""):
        calls["n"] += 1
        if calls["n"] <= 1:  # collection consent
            return "yes"
        raise EOFError()  # identity approval prompt -> EOF

    monkeypatch.setattr("builtins.input", seq_input)
    client = FakeClient(_script())
    rc = await driver.run(client, _args())
    assert rc == 1  # stopped, not quoted
    assert any(c[0] == "DELETE" and c[1] == "/api/v1/browser/sessions/bs-1" for c in client.calls)


async def test_driver_user_quit_stops_before_browser(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "quit")
    client = FakeClient(_script())
    rc = await driver.run(client, _args())
    assert rc == 3
    assert all("/api/v1/browser/sessions" not in p for p in client.paths())


async def test_driver_quote_terminates_and_calls_normalized_endpoint(monkeypatch) -> None:
    client = FakeClient(_script())
    rc = await driver.run(client, _args())
    assert rc == 0
    assert any(p == "/api/v1/browser/sessions/bs-1/quote" for p in client.paths())
    assert any(c[0] == "DELETE" and c[1] == "/api/v1/browser/sessions/bs-1" for c in client.calls)
