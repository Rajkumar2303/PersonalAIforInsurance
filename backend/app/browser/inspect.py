"""Generic visible-form inspector (Issue #7).

Inspects visible interactive controls (input/select/textarea/radio/checkbox)
and extracts SAFE metadata only: label text, aria-label, name, id, type,
placeholder, required, options labels. It NEVER extracts existing input values
and NEVER logs DOM innerHTML/page HTML wholesale.

Radio/checkbox groups are grouped by ``name`` into a single field observation
with ``options_labels`` so the executor can fill the group deterministically.

Issue #8.5 (Smoke #1b): also inspects visible interactive ELEMENTS (button,
a, [role=button/radio/option/checkbox/link/tab]) for discovery, preferring
accessible roles/names over brittle CSS. Metadata is safe (no values).
"""

from __future__ import annotations

from typing import Any, Optional

from ..models.browser.observation import (
    BrowserFieldObservation,
    BrowserInteractiveElement,
    BrowserPageObservation,
)

# Selectors used to discover interactive elements (accessible-role based).
_INTERACTIVE_SELECTORS = (
    "button",
    "a",
    '[role="button"]',
    '[role="radio"]',
    '[role="option"]',
    '[role="checkbox"]',
    '[role="link"]',
    '[role="tab"]',
)

# Allowlist of interaction-relevant aria attributes (never full attr dump).
_ARIA_ALLOWLIST = (
    "aria-label",
    "aria-checked",
    "aria-selected",
    "aria-expanded",
    "aria-pressed",
    "aria-required",
    "aria-invalid",
    "aria-haspopup",
    "aria-controls",
)


class PageInspector:
    """Deterministic, safe inspector over a Playwright page."""

    async def inspect(self, page: Any, page_index: int = 0) -> BrowserPageObservation:
        fields: list[BrowserFieldObservation] = []
        await self._inspect_text_inputs(page, fields)
        await self._inspect_radio_groups(page, fields)
        await self._inspect_checkbox_groups(page, fields)
        interactives = await self._inspect_interactives(page)
        signature_heading = await self._signature_heading(page)
        return BrowserPageObservation(
            page_index=page_index,
            page_signature=None,
            fields=fields,
            interactives=interactives,
            controls_count=len(fields),
            interactives_count=len(interactives),
            heading=signature_heading,
        )

    async def _inspect_interactives(self, page: Any) -> list[BrowserInteractiveElement]:
        """Discover visible interactive elements (safe metadata, no values).

        Prefers accessible roles/names; dedupes overlapping matches by
        (element_type, role, accessible_name, external_id).
        """
        seen: set[tuple] = set()
        elements: list[BrowserInteractiveElement] = []
        for selector in _INTERACTIVE_SELECTORS:
            locator = page.locator(selector)
            count = await locator.count()
            for i in range(min(count, 200)):
                control = locator.nth(i)
                try:
                    if not await control.is_visible():
                        continue
                    aria_label = await control.get_attribute("aria-label")
                    text = (await control.inner_text()).strip() if aria_label is None else ""
                    accessible_name = (aria_label or text or "").strip()
                    if not accessible_name:
                        continue
                    tag = (await control.evaluate("el => el.tagName.toLowerCase()")) or "unknown"
                    role = await control.get_attribute("role")
                    element_type = role if role else tag
                    element_id = await control.get_attribute("id") or await control.get_attribute("name")
                    disabled = await control.is_disabled()
                    aria: dict[str, str] = {}
                    for attr in _ARIA_ALLOWLIST:
                        value = await control.get_attribute(attr)
                        if value:
                            aria[attr] = value[:80]
                    key = (tag, role, accessible_name[:80], element_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    elements.append(
                        BrowserInteractiveElement(
                            accessible_name=accessible_name[:200],
                            role=role,
                            element_type=element_type,
                            external_id=element_id,
                            disabled=disabled,
                            aria=aria,
                        )
                    )
                except Exception:
                    continue
        return elements

    async def _inspect_text_inputs(self, page: Any, out: list[BrowserFieldObservation]) -> None:
        for selector in ("input:not([type=radio]):not([type=checkbox]):not([type=hidden])", "select", "textarea"):
            locator = page.locator(selector)
            count = await locator.count()
            for i in range(count):
                control = locator.nth(i)
                if not await self._is_fillable(control):
                    continue
                field = await self._field_for_control(page, control)
                if field is not None:
                    out.append(field)

    @staticmethod
    async def _is_fillable(control: Any) -> bool:
        """A control is inspected only when visible, enabled and not readonly.

        Hidden/disabled/readonly controls are ignored - they are not real
        questions the browser should answer.
        """
        try:
            if not await control.is_visible():
                return False
            if not await control.is_enabled():
                return False
            readonly = await control.get_attribute("readonly")
            if readonly is not None:
                return False
            return True
        except Exception:
            return False

    async def _inspect_radio_groups(self, page: Any, out: list[BrowserFieldObservation]) -> None:
        radios = page.locator("input[type=radio]")
        count = await radios.count()
        groups: dict[str, list[Any]] = {}
        for i in range(count):
            radio = radios.nth(i)
            if not await self._is_fillable(radio):
                continue
            name = await radio.get_attribute("name") or ""
            groups.setdefault(name, []).append(radio)
        for name, controls in groups.items():
            field = await self._group_field(page, "radio", name, controls)
            if field is not None:
                out.append(field)

    async def _inspect_checkbox_groups(self, page: Any, out: list[BrowserFieldObservation]) -> None:
        boxes = page.locator("input[type=checkbox]")
        count = await boxes.count()
        groups: dict[str, list[Any]] = {}
        for i in range(count):
            box = boxes.nth(i)
            if not await self._is_fillable(box):
                continue
            name = await box.get_attribute("name") or ""
            groups.setdefault(name, []).append(box)
        for name, controls in groups.items():
            field = await self._group_field(page, "checkbox", name, controls)
            if field is not None:
                out.append(field)

    async def _field_for_control(self, page: Any, control: Any) -> Optional[BrowserFieldObservation]:
        control_type = (await control.evaluate("el => el.tagName.toLowerCase()")) or "input"
        fid = await control.get_attribute("id")
        name = await control.get_attribute("name")
        aria = await control.get_attribute("aria-label")
        placeholder = await control.get_attribute("placeholder")
        input_type = await control.get_attribute("type")
        required = (await control.get_attribute("required")) is not None
        label = await self._label_for(page, fid, control)
        external_id = fid or name or self._id_from_label(label)
        options: list[str] = []
        if control_type == "select":
            options = await self._option_labels(control)
        return BrowserFieldObservation(
            external_field_id=external_id or "unknown",
            control_type=control_type,
            label=label,
            name=name,
            input_type=input_type,
            placeholder=placeholder,
            required=required,
            options_labels=options,
        )

    async def _group_field(
        self, page: Any, kind: str, name: str, controls: list[Any]
    ) -> Optional[BrowserFieldObservation]:
        labels: list[str] = []
        first_id: Optional[str] = None
        legend = await self._fieldset_legend(page, controls[0])
        for control in controls:
            fid = await control.get_attribute("id")
            first_id = first_id or fid
            label = await self._label_for(page, fid, control)
            if label:
                labels.append(label)
        group_label = legend or (labels[0] if labels else None)
        external_id = first_id or name or self._id_from_label(group_label)
        return BrowserFieldObservation(
            external_field_id=external_id or "unknown",
            control_type=kind,
            label=group_label,
            name=name,
            input_type=kind,
            required=False,
            options_labels=labels,
        )

    @staticmethod
    async def _fieldset_legend(page: Any, control: Any) -> Optional[str]:
        try:
            legend = control.locator("xpath=ancestor::fieldset[1]/legend").first
            if await legend.count():
                text = (await legend.inner_text()).strip()
                if text:
                    return text
        except Exception:
            pass
        return None

    async def _label_for(self, page: Any, fid: Optional[str], control: Any) -> Optional[str]:
        aria = await control.get_attribute("aria-label")
        if aria and aria.strip():
            return aria.strip()
        if fid:
            label_for = page.locator(f'label[for="{fid}"]').first
            try:
                if await label_for.is_visible():
                    text = (await label_for.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                pass
        try:
            parent_label = control.locator("xpath=ancestor::label[1]").first
            if await parent_label.count():
                text = (await parent_label.inner_text()).strip()
                if text:
                    return text
        except Exception:
            pass
        return None

    @staticmethod
    async def _option_labels(control: Any) -> list[str]:
        options = control.locator("option")
        count = await options.count()
        labels: list[str] = []
        for i in range(count):
            text = (await options.nth(i).inner_text()).strip()
            if text:
                labels.append(text)
        return labels

    @staticmethod
    async def _signature_heading(page: Any) -> Optional[str]:
        """Return the first visible heading, only used for signature matching.

        The observation keeps this heading ONLY when the signature detector
        confirms it matches a known page-signature heading pattern; the
        executor never stores arbitrary page text.
        """
        try:
            heading = page.locator("h1, h2, h3").first
            if await heading.count():
                text = (await heading.inner_text()).strip()
                return text[:200] or None
        except Exception:
            pass
        return None

    @staticmethod
    def _id_from_label(label: Optional[str]) -> str:
        if not label:
            return "unknown"
        import re

        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        return slug or "unknown"
