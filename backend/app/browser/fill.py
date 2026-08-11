"""Controlled fill strategies + transforms (Issue #7).

Deterministic, reusable fill strategies for text/integer/select/radio/checkbox/
date/yes-no. Transformations use a CONTROLLED enum registry - no arbitrary
Python/eval from config. ``option_map`` and ``date_format`` are the only
config-driven transformation data (canonical value -> website label).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from ..models.browser.config import BrowserFieldBinding, FillStrategy, TransformKind
from ..models.browser.observation import BrowserFieldObservation


def _css_escape(value: str) -> str:
    """Minimal CSS identifier escaping for safe locator construction."""
    return "".join("\\" + c if not (c.isalnum() or c in "-_") else c for c in value)


class FillError(RuntimeError):
    """Raised when a value cannot be transformed or a control cannot be filled.

    Error text is SAFE - it never contains the value being filled.
    """


class OptionNotSupportedError(FillError):
    """Raised when the destination has no compatible option for the canonical
    value. The executor must NOT pick an arbitrary closest option - it pauses
    with a ``value_not_supported`` observation (Issue #8 classifies later)."""


def transform_value(value: Any, binding: BrowserFieldBinding) -> str:
    """Deterministically transform a canonical value for the destination."""
    kind = binding.transform
    if kind is TransformKind.NONE:
        return "" if value is None else str(value)
    if kind is TransformKind.INTEGER_TO_STRING:
        return str(int(value))
    if kind is TransformKind.COLLECTION_LENGTH:
        # ``value`` is the integer collection length derived by the value source
        # (e.g. product_data.vehicles -> len). Counts stay in sync with the
        # actual canonical collection and can never drift from its length.
        return str(int(value))
    if kind is TransformKind.BOOL_TO_YES_NO:
        return "Yes" if value else "No"
    if kind is TransformKind.ISO_DATE_TO_DEST:
        return _format_date(value, binding.date_format)
    if kind is TransformKind.ENUM_TO_LABEL:
        key = str(value)
        if isinstance(value, float) and value.is_integer():
            key = str(int(value))
        return binding.option_map.get(key, str(value))
    return "" if value is None else str(value)


def _format_date(value: Any, date_format: Optional[str]) -> str:
    if isinstance(value, dt.datetime):
        return value.strftime(date_format or "%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.strftime(date_format or "%Y-%m-%d")
    try:
        parsed = dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise FillError("invalid date value") from exc
    return parsed.strftime(date_format or "%Y-%m-%d")


class FieldFiller:
    """Fills one mapped field using a Playwright page (safe)."""

    async def fill(self, page: Any, obs: BrowserFieldObservation, binding: BrowserFieldBinding, value: Any) -> None:
        transformed = transform_value(value, binding)
        strategy = binding.fill_strategy
        if strategy is FillStrategy.RADIO:
            await self._fill_radio(page, obs, binding, transformed)
            return
        if strategy is FillStrategy.YES_NO:
            await self._fill_yes_no(page, obs, binding, transformed)
            return
        if strategy is FillStrategy.CHECKBOX:
            await self._fill_checkbox(page, bool(value))
            return
        locator = await self._locate(page, obs)
        if strategy is FillStrategy.SELECT:
            await self._fill_select(page, obs, transformed)
            return
        await locator.fill(transformed)

    async def _fill_select(self, page: Any, obs: BrowserFieldObservation, target: str) -> None:
        locator = await self._locate(page, obs)
        labels = [
            (await locator.locator("option").nth(i).inner_text()).strip()
            for i in range(await locator.locator("option").count())
        ]
        if not any(target.lower() == o.lower() for o in labels):
            raise OptionNotSupportedError("destination select has no compatible option")
        await locator.select_option(label=target)

    async def _fill_radio(self, page: Any, obs: BrowserFieldObservation, binding: BrowserFieldBinding, target: str) -> None:
        await self._click_option_by_label(page, obs, "radio", target)

    async def _fill_yes_no(self, page: Any, obs: BrowserFieldObservation, binding: BrowserFieldBinding, target: str) -> None:
        # Prefer a radio group labelled Yes/No; fall back to a select.
        radios = page.locator("input[type=radio]")
        count = await radios.count()
        name = obs.name
        for i in range(count):
            radio = radios.nth(i)
            radio_name = await radio.get_attribute("name")
            if name and radio_name != name:
                continue
            label = await self._control_label(page, radio)
            if label and label.strip().lower() == target.lower():
                await radio.check()
                return
        # fall back to select with Yes/No option labels
        select = page.locator("select")
        count = await select.count()
        for i in range(count):
            option_labels = [
                (await select.nth(i).locator("option").nth(j).inner_text()).strip()
                for j in range(await select.nth(i).locator("option").count())
            ]
            if any(target.lower() == o.lower() for o in option_labels):
                await select.nth(i).select_option(label=target)
                return
        raise OptionNotSupportedError("destination has no compatible yes/no option")

    async def _fill_checkbox(self, page: Any, value: bool) -> None:
        # Single-checkbox case; group checkboxes are handled by the mapper as a
        # control_type=checkbox group, but bool fills target the primary.
        box = page.locator("input[type=checkbox]").first
        if value:
            await box.check()
        else:
            await box.uncheck()

    async def _click_option_by_label(self, page: Any, obs: BrowserFieldObservation, kind: str, target: str) -> None:
        controls = page.locator(f"input[type={kind}]")
        count = await controls.count()
        name = obs.name
        for i in range(count):
            control = controls.nth(i)
            radio_name = await control.get_attribute("name")
            if name and radio_name != name:
                continue
            label = await self._control_label(page, control)
            if label and label.strip().lower() == target.lower():
                await control.check()
                return
        raise OptionNotSupportedError("destination has no compatible option")

    async def _control_label(self, page: Any, control: Any) -> Optional[str]:
        fid = await control.get_attribute("id")
        if fid:
            label_for = page.locator(f'label[for="{fid}"]').first
            try:
                if await label_for.count():
                    text = (await label_for.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                pass
        return None

    async def _locate(self, page: Any, obs: BrowserFieldObservation):
        """Locate the control deterministically: prefer the DOM id, then name,
        then label (scoped to enabled, non-readonly controls)."""
        if obs.external_field_id and obs.external_field_id != "unknown":
            by_id = page.locator(f"#{_css_escape(obs.external_field_id)}")
            try:
                if await by_id.count():
                    return by_id.first
            except Exception:
                pass
        if obs.name:
            by_name = page.locator(f"[name={_css_escape(obs.name)!r}]")
            try:
                if await by_name.count():
                    return by_name.first
            except Exception:
                pass
        if obs.label:
            return (
                page.get_by_label(obs.label, exact=True)
                .locator("input:enabled:not([readonly]), select:enabled, textarea:enabled:not([readonly])")
                .first
            )
        raise FillError("cannot locate control")
