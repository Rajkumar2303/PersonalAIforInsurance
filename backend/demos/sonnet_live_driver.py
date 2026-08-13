"""Sonnet LIVE operator driver - HTTP client of the RUNNING backend.

This driver is a thin client of the already-running backend (FastAPI on
``--base-url``). It does NOT create its own IntakeEngine, applicant profile,
consent store, BrowserManager, or evidence store. It uses the existing intake,
planner, consent, direct browser-session and normalized-quote APIs, and the
backend executor fills all values just-in-time from the applicant's real intake
session. The driver NEVER downloads, prints or copies applicant values.

Flow:
  1. validate ``--intake-session-id`` via GET /intake/sessions/{id}
  2. confirm the Sonnet route is ready via GET /planner/plan?mode=live
  3. consent: if collection / Sonnet route-disclosure consent is missing, show
     the existing disclosure and record it via the consent APIs ONLY after the
     participant types YES (never fabricates consent, no comparison run, no
     Square One)
  4. start ONE headed live Sonnet browser session (planned_route_id=sonnet)
  5. loop: mapped questions are processed automatically; on pause (missing /
     unknown / identity checkpoint) the browser stays open and the participant
     answers/resumes; identity_lookup requires an explicit YES + approve +
     resume on the SAME browser_session_id + attempt_id
  6. stop at declaration / CAPTCHA / signature / payment / purchase / binding
  7. on an explicit quote, call the normalized-quote endpoint, print only the
     safe result + evidence ids, then close Chromium

Safety: LIVE mode requires explicit --personal-use and --accurate-info
attestations; never falls back to any pilot/test profile.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

REGISTRY_ID = "sonnet"

_RESUMABLE_PAUSES = {
    "paused_needs_field",
    "paused_unknown_field",
    "paused_needs_consent",
    "paused_ambiguous",
    "paused_value_not_supported",
    "paused_validation_error",
}

_TERMINAL_STOPS = {
    "stopped_access_control",
    "stopped_prohibited",
    "stopped_human_checkpoint",
    "stopped_unexpected_host",
    "failed",
}


class BackendError(Exception):
    """A backend API error with a safe detail (never applicant values)."""


class _BackendClient:
    """Tiny stdlib HTTP client for the backend API (no new dependencies)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: Optional[dict] = None) -> Any:
        return self._request("POST", path, body or {})

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                detail = payload.get("detail", "") if isinstance(payload, dict) else ""
            except Exception:
                pass
            raise BackendError(detail or f"HTTP {exc.code}") from exc


def _friendly_blockers(route: Optional[dict]) -> str:
    if not route:
        return "sonnet route not found"
    blockers = [b.get("kind") if isinstance(b, dict) else b for b in (route.get("blockers") or [])]
    if not blockers:
        return "not ready"
    return ", ".join(str(b) for b in blockers)


def _ask_yes(prompt: str) -> bool:
    """Explicit YES gate. True only for an exact 'yes'; ANY other input
    ('no', 'quit', EOF, Ctrl+C, or unavailable stdin) returns False and stops
    the driver. Never re-prompts indefinitely.
    """
    try:
        answer = input(f"    >> {prompt} Type YES to continue, or 'quit': ").strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        return False
    return answer == "yes"


def _consent_flow(client: _BackendClient, intake_session_id: str, plan: dict) -> bool:
    """Ensure collection + Sonnet route consent exist. Returns True when ready.

    Records consent ONLY after an explicit YES; never fabricates consent; does
    not create a comparison run or start any provider.
    """
    route = next((r for r in plan.get("routes", []) if r.get("registry_id") == REGISTRY_ID), None)
    blockers = [str(b.get("kind") if isinstance(b, dict) else b) for b in (route.get("blockers") or [])]
    consent_required = "consent_required" in blockers

    # Collection consent state is not exposed by the API, so we conservatively
    # ask (recording is idempotent) rather than assume it is granted.
    print("[driver] Collection consent: not assumed - requesting explicit consent.")
    if not _ask_yes("Your profile data will be collected only for this quote run."):
        print("[driver] Collection consent declined - stopping.")
        return False
    client.post(f"/api/v1/intake/sessions/{intake_session_id}/consent", {"scope": "collection"})

    if consent_required:
        print("[driver] Sonnet route-disclosure consent is required.")
        try:
            disclosure = client.post(
                f"/api/v1/intake/sessions/{intake_session_id}/route-disclosure?mode=live",
                {"registry_id": REGISTRY_ID, "paths": []},
            )
            paths = [c.get("canonical_path") for c in disclosure.get("path_details", [])]
            print(f"    disclosure paths: {', '.join(paths) if paths else '(route-wide)'}")
        except BackendError as exc:
            print(f"    (disclosure unavailable: {exc})")
        if not _ask_yes("Share the disclosed Sonnet fields with Sonnet for this quote run."):
            print("[driver] Sonnet route consent declined - stopping.")
            return False
        client.post(
            f"/api/v1/intake/sessions/{intake_session_id}/consent/route?mode=live",
            {"registry_id": REGISTRY_ID, "paths": [], "granted": True},
        )
    return True


def _print_safe_quote(quote: dict, browser_session_id: str, attempt_id: Optional[str]) -> None:
    premium = quote.get("premium") or {}
    print("    QUOTE (LIVE - Sonnet)")
    print(f"      premium: {premium.get('normalized_annual_amount')} {premium.get('currency')} "
          f"({premium.get('provider_presented_frequency') or 'annual'})")
    for item in (quote.get("coverage_ledger") or {}).get("items", []):
        print(f"      coverage: {item.get('item_key')} = {item.get('state')}")
    print(f"      normalized_quote_id: {quote.get('normalized_quote_id')}")
    print(f"      observed_at: {quote.get('normalized_at')}")
    print(f"      browser_session_id: {browser_session_id}  attempt_id: {attempt_id or 'n/a'}")


async def run(client: _BackendClient, args: argparse.Namespace) -> int:
    intake_session_id = args.intake_session_id

    # 1) validate the intake session exists (friendly stop if not).
    try:
        session = client.get(f"/api/v1/intake/sessions/{intake_session_id}")
    except BackendError as exc:
        print(f"[driver] STOP: intake session not found or unavailable ({exc}).")
        print("         Copy the intake_session_id from the backend terminal and retry.")
        return 2

    # 2) existing live planner: confirm the Sonnet route is ready.
    try:
        plan = client.get(f"/api/v1/planner/plan?session_id={intake_session_id}&mode=live")
    except BackendError as exc:
        print(f"[driver] STOP: could not plan the Sonnet route ({exc}).")
        return 3
    route = next((r for r in plan.get("routes", []) if r.get("registry_id") == REGISTRY_ID), None)
    if route is None:
        print("[driver] STOP: Sonnet route not found in the plan.")
        return 3
    if not route.get("is_ready"):
        blockers = _friendly_blockers(route)
        print(f"[driver] Sonnet route is not ready. Friendly blockers: {blockers}")
        if "consent_required" in blockers:
            if not _consent_flow(client, intake_session_id, plan):
                return 3
            plan = client.get(f"/api/v1/planner/plan?session_id={intake_session_id}&mode=live")
            route = next((r for r in plan.get("routes", []) if r.get("registry_id") == REGISTRY_ID), None)
            if route is None or not route.get("is_ready"):
                print(f"[driver] STOP: still not ready. Friendly blockers: {_friendly_blockers(route)}")
                return 3
        else:
            return 3
    # Confirm collection consent explicitly (idempotent) even when already ready.
    if not _consent_flow(client, intake_session_id, plan):
        return 3

    print(f"[driver] intake_session_id={intake_session_id} sonnet_ready=True")

    # 3) start ONE direct live Sonnet browser session with explicit attestations.
    live_gate = {
        "personal_use_confirmed": True,
        "accurate_information_attested": True,
        "attested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    try:
        started = client.post("/api/v1/browser/sessions", {
            "intake_session_id": intake_session_id,
            "planned_route_id": REGISTRY_ID,
            "execution_mode": "live",
            "live_gate": live_gate,
        })
    except BackendError as exc:
        print(f"[driver] STOP: could not start the Sonnet browser session ({exc}).")
        return 5
    if not started.get("started"):
        refusal = started.get("refusal") or {}
        print(f"[driver] STOP: browser refused ({refusal.get('reason')}) - {refusal.get('detail')}")
        return 5
    browser_session_id = started["session"]["browser_session_id"]
    attempt_id = started["session"].get("attempt_id")
    print(f"[driver] browser_session_id={browser_session_id} attempt_id={attempt_id or 'n/a'}")

    # 4) run + operator loop (same session/attempt throughout). Guarded
    # against stalls: the identical paused state may not repeat more than twice
    # without progress, and there is a hard max-step cap. Chromium is ALWAYS
    # closed on quote, terminal, stall, user quit, EOF/Ctrl+C or any error.
    last_pause_key: Optional[tuple] = None
    same_pause_count = 0
    stalled = False
    quoted = False
    try:
        result = client.post(f"/api/v1/browser/sessions/{browser_session_id}/run")
        step = 0
        while result is not None:
            step += 1
            session_state = result.get("session") or {}
            step_state = result.get("step") or {}
            status = (step_state.get("status") or session_state.get("status") or "running")
            obs = step_state.get("observation") or {}
            # The backend preserves the attempt_id on the session once running;
            # print the REAL value, never a placeholder.
            attempt_id = session_state.get("attempt_id") or attempt_id
            print(f"[driver] step={step} status={status} attempt_id={attempt_id or 'n/a'}")

            # --- no-progress stall guard -----------------------------
            pause_key: Optional[tuple] = None
            if status == "paused_human_checkpoint":
                checkpoint = obs.get("checkpoint") or {}
                pause_key = (status, checkpoint.get("checkpoint_type"), ())
            elif status in _RESUMABLE_PAUSES:
                missing = tuple(obs.get("missing_field_paths") or obs.get("pending_field_paths") or [])
                pause_key = (status, None, missing)
            else:
                last_pause_key = None
                same_pause_count = 0
            if pause_key is not None:
                if pause_key == last_pause_key:
                    same_pause_count += 1
                else:
                    same_pause_count = 1
                last_pause_key = pause_key
                if same_pause_count > 2:
                    print(f"[driver] stalled_no_progress - the same paused state "
                          f"{pause_key[0]!r} repeated {same_pause_count} times without progress.")
                    stalled = True
                    break

            if obs.get("quote") and obs["quote"].get("quote_present"):
                try:
                    quote = client.get(f"/api/v1/browser/sessions/{browser_session_id}/quote")
                except BackendError as exc:
                    print(f"[driver] quote endpoint error: {exc}")
                    break
                _print_safe_quote(quote, browser_session_id, attempt_id)
                quoted = True
                break

            if status == "paused_human_checkpoint":
                checkpoint = obs.get("checkpoint") or {}
                ctype = checkpoint.get("checkpoint_type")
                if checkpoint.get("must_not_automate") or ctype in (
                    "application_declaration", "signature", "payment", "purchase", "policy_binding",
                ):
                    print(f"[driver] STOP: {ctype or 'human checkpoint'} must not be automated.")
                    break
                if ctype == "identity_lookup":
                    if not _ask_yes(
                        "Approve submitting your licence / triggering the identity lookup "
                        "(no values are shown)."
                    ):
                        print("[driver] not approved - stopping.")
                        break
                    client.post(f"/api/v1/browser/sessions/{browser_session_id}/approve-checkpoint",
                                {"checkpoint_type": ctype})
                    result = client.post(f"/api/v1/browser/sessions/{browser_session_id}/resume")
                    continue
                if not _ask_yes(f"Resolve the '{ctype}' checkpoint in the browser, then approve."):
                    print("[driver] not approved - stopping.")
                    break
                result = client.post(f"/api/v1/browser/sessions/{browser_session_id}/resume")
                continue

            if status in _RESUMABLE_PAUSES:
                if step >= args.max_steps:
                    print(f"[driver] max_steps={args.max_steps} reached - stopping.")
                    break
                missing = obs.get("missing_field_paths") or obs.get("pending_field_paths") or []
                print("    Complete the current question accurately in the Sonnet browser, then Resume.")
                if missing:
                    print(f"    waiting on canonical fields: {', '.join(missing)}")
                if not _ask_yes("Resume after you have answered accurately in the browser."):
                    print("[driver] not resumed - stopping.")
                    break
                result = client.post(f"/api/v1/browser/sessions/{browser_session_id}/resume")
                continue

            if status in _TERMINAL_STOPS or status == "succeeded":
                print(f"[driver] TERMINAL status={status} - stopping (no auto-retry).")
                break

            # 'running' / 'fields_filled' -> next step.
            if step >= args.max_steps:
                print(f"[driver] max_steps={args.max_steps} reached - stopping.")
                break
            result = client.post(f"/api/v1/browser/sessions/{browser_session_id}/resume")
    except (EOFError, KeyboardInterrupt, OSError, BackendError) as exc:  # pragma: no cover - safety net
        print(f"[driver] stopped: {type(exc).__name__}: {exc}")
    finally:
        # 5) always close Chromium after quote, terminal, stall, quit or error.
        try:
            client.delete(f"/api/v1/browser/sessions/{browser_session_id}")
            print("[driver] Chromium closed.")
        except Exception:
            print("[driver] note: could not close browser.")
    return 7 if stalled else (0 if quoted else 1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sonnet LIVE operator driver (client of the running backend)"
    )
    parser.add_argument("--intake-session-id", required=True,
                        help="The intake session created in the frontend (from the backend terminal)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000",
                        help="Backend base URL (default http://127.0.0.1:8000)")
    parser.add_argument("--registry-id", default=REGISTRY_ID)
    parser.add_argument("--max-steps", type=int, default=40, help="Cap on resume steps (safety)")
    parser.add_argument("--personal-use", action="store_true",
                        help="REQUIRED attestation: this is your personal use")
    parser.add_argument("--accurate-info", action="store_true",
                        help="REQUIRED attestation: profile uses your own accurate info")
    args = parser.parse_args()

    if not (args.personal_use and args.accurate_info):
        print("[driver] ERROR: LIVE execution requires BOTH --personal-use and --accurate-info.")
        return 3

    client = _BackendClient(args.base_url)
    try:
        return asyncio.run(run(client, args))
    except BackendError as exc:  # pragma: no cover - operator-facing resilience
        print(f"[driver] backend error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
