from __future__ import annotations

from typing import Any

import pytest

from pilot.system import browser


class _FakePage:
    def __init__(self) -> None:
        self.click_calls: list[tuple[str, dict[str, Any]]] = []
        self.waits: list[int] = []

    async def click(self, selector: str, **kwargs: Any) -> None:
        self.click_calls.append((selector, kwargs))

    async def wait_for_timeout(self, timeout: int) -> None:
        self.waits.append(timeout)


class _FakeLocator:
    def __init__(self, label: str, *, visible: bool = True) -> None:
        self.label = label
        self.visible = visible
        self.click_calls: list[dict[str, Any]] = []

    async def is_visible(self) -> bool:
        return self.visible

    async def inner_text(self) -> str:
        return self.label

    async def get_attribute(self, name: str) -> str | None:
        return None

    async def click(self, **kwargs: Any) -> None:
        self.click_calls.append(kwargs)


class _FakeLocatorList:
    def __init__(self, items: list[_FakeLocator]) -> None:
        self.items = items

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> _FakeLocator:
        return self.items[index]


class _SemanticFallbackPage(_FakePage):
    def __init__(self, labels: list[str]) -> None:
        super().__init__()
        self.items = [_FakeLocator(label) for label in labels]

    async def click(self, selector: str, **kwargs: Any) -> None:
        self.click_calls.append((selector, kwargs))
        raise TimeoutError("Timeout 10000ms exceeded.")

    def locator(self, selector: str) -> _FakeLocatorList:
        assert selector == browser._INTERACTIVE_SELECTOR
        return _FakeLocatorList(self.items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exact", "expected_selector"),
    [
        (False, "text=More information"),
        (True, "text='More information'"),
    ],
)
async def test_click_text_dispatches_without_waiting_for_navigation(
    monkeypatch: pytest.MonkeyPatch,
    exact: bool,
    expected_selector: str,
) -> None:
    page = _FakePage()

    async def fake_get_page(tab_index: int = -1) -> _FakePage:
        return page

    monkeypatch.setattr(browser, "_get_page", fake_get_page)
    monkeypatch.setattr(browser, "DOM_DIFF_ENABLED", False)

    result = await browser.browser_click_text("More information", exact=exact)

    assert result == "Clicked element with text: More information"
    assert page.click_calls == [
        (
            expected_selector,
            {
                "timeout": 1000,
                "no_wait_after": True,
            },
        )
    ]
    assert page.waits == [300]


def test_semantic_click_match_maps_more_information_to_learn_more() -> None:
    assert browser._select_semantic_click_candidate(
        "More information",
        ["Contact", "Learn more", "Privacy"],
    ) == (1, "Learn more")


def test_semantic_click_match_refuses_ambiguous_choices() -> None:
    assert (
        browser._select_semantic_click_candidate(
            "Continue",
            ["Proceed to checkout", "Proceed without account"],
        )
        is None
    )


@pytest.mark.asyncio
async def test_click_text_uses_unambiguous_semantic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _SemanticFallbackPage(["Contact", "Learn more", "Privacy"])

    async def fake_get_page(tab_index: int = -1) -> _SemanticFallbackPage:
        return page

    monkeypatch.setattr(browser, "_get_page", fake_get_page)
    monkeypatch.setattr(browser, "DOM_DIFF_ENABLED", False)

    result = await browser.browser_click_text("More information")

    assert result == "Clicked closest available option: Learn more (requested: More information)"
    assert page.items[1].click_calls == [{"timeout": 10000, "no_wait_after": True}]
    assert page.waits == [300]
