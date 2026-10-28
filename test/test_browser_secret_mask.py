"""Mask Secrets: what the page shows of a quick-ask value never reaches the agent.

The gateway keeps a copy of the values it typed (``browser_harness.secret_mask``)
and masks them at the exits page content leaves by: walker reads, op results, the
op route's error text, screenshots for the agent and the decision model, and the
turn replay's frames. The person's own live view is never masked by value.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from browser_live_helpers import frame_event
from PIL import Image
from test_browser_turn_replay import TURN, all_bytes, jpeg, make_recorder, probe_stream

from kiro_crew.browser_harness import ops
from kiro_crew.browser_harness import secret_mask as sm

KEY = "chat-1"
PASSWORD = "Hunter2-Canary"
EMAIL = "Octo.Cat@example.com"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    sm._HELD.clear()
    sm._WORLDS.clear()
    yield
    sm._HELD.clear()
    sm._WORLDS.clear()


def _ask(kind: str = "credentials", origin: str = "https://login.example.com", top: str = ""):
    return SimpleNamespace(spec=SimpleNamespace(kind=kind), origin=origin, top_origin=top or origin)


def _hold(**values: str) -> None:
    sm.hold(KEY, _ask(), values or {"username": EMAIL, "password": PASSWORD})


# ── what is held and how text is masked ──────────────────────────────────────


def test_every_form_of_a_held_value_is_masked_in_every_string() -> None:
    _hold()
    shown = {
        "title": f"Inbox - {EMAIL.lower()}",
        "url": "https://login.example.com/?hint=octo.cat%40example.com&x=1",
        "nodes": [{"name": f"Signed in as {EMAIL.upper()}"}, {"value": f"<b>{PASSWORD}</b>"}],
        "console": [f'{{"pw": "{PASSWORD}"}}', "unrelated"],
        "plus": "q=octo.cat%40example.com",
    }
    out = json.dumps(sm.redact(KEY, shown))
    assert PASSWORD.lower() not in out.lower() and "octo.cat" not in out.lower()
    assert "unrelated" in out and sm.MASK in out


def test_only_sign_in_and_code_values_are_held_and_short_ones_are_not_matched() -> None:
    sm.hold(KEY, _ask("text"), {"text": "Alice Smith"})
    assert sm.live(KEY) is None
    sm.hold(KEY, _ask("otp"), {"otp": "482913"})
    assert sm.redact(KEY, "code 482913 sent") == f"code {sm.MASK} sent"
    sm.hold(KEY, _ask(), {"username": "bob", "password": PASSWORD})
    # A short value is masked as a whole word only.
    assert sm.redact(KEY, "Hi Bob, bobcat") == f"Hi {sm.MASK}, bobcat"


def test_protocol_keys_survive_so_the_guards_still_read_them() -> None:
    sm.hold(KEY, _ask(), {"username": "type", "password": "password"})
    # Keys are never rewritten, and the guards' own reads are not masked at all.
    assert sm.redact(KEY, {"password": True, "label": "type"}) == {
        "password": True,
        "label": sm.MASK,
    }


def test_a_value_cut_short_by_a_cap_is_masked_and_protocol_values_are_kept() -> None:
    sm.hold(KEY, _ask("otp"), {"otp": "482913"})
    pad = "x" * 40
    assert sm.redact(KEY, f"{pad} code 48291\u2026") == f"{pad} code {sm.MASK}\u2026"
    assert sm.redact(KEY, f"{pad} 482") == f"{pad} {sm.MASK}"  # 3+ leading characters
    assert sm.redact(KEY, f"{pad} 48") == f"{pad} 48"
    assert sm.redact(KEY, "abc 482") == "abc 482"  # too short to have been cut
    out = sm.redact(KEY, {"code": "e482913", "ref": "e482913", "error": "e482913"})
    assert out == {"code": "e482913", "ref": "e482913", "error": f"e{sm.MASK}"}
    # Only an op result's own top level: a caller-named read called "code" is page text.
    nested = sm.redact(KEY, {"state": {"read": {"code": "482913"}}})
    assert nested == {"state": {"read": {"code": sm.MASK}}}


def test_the_copy_runs_out_and_is_dropped_on_another_origin() -> None:
    sm.hold(KEY, _ask(), {"password": PASSWORD}, now=100.0)
    assert sm.live(KEY, now=100.0 + sm.HOLD_S - 1) is not None
    assert sm.live(KEY, now=100.0 + sm.HOLD_S) is None
    assert KEY not in sm._HELD
    _hold()
    sm.seen_url(KEY, "https://login.example.com/home")
    assert sm.live(KEY) is not None
    assert sm.live(KEY) is not None
    sm.seen_url(KEY, "https://other.example.net/")
    assert sm.live(KEY) is None
    assert sm.redact(KEY, PASSWORD) == PASSWORD  # nothing held, nothing to mask


def test_a_second_step_on_the_origin_joins_the_copy_and_another_origin_replaces_it() -> None:
    _hold()
    sm.hold(KEY, _ask("otp"), {"otp": "123456"})  # the sign-in's one-time code
    sm.hold(KEY, _ask("text"), {"text": "Alice"})  # holds nothing, drops nothing
    assert sm.redact(KEY, f"{EMAIL} {PASSWORD} 123456") == f"{sm.MASK} {sm.MASK} {sm.MASK}"
    sm.hold(KEY, _ask("otp", origin="https://other.example.net"), {"otp": "654321"})
    assert sm.redact(KEY, f"{PASSWORD} 654321") == f"{PASSWORD} {sm.MASK}"
    assert sm.redact("chat-2", "654321") == "654321"  # per session


# ── the op exits ────────────────────────────────────────────────────────────


def _ctx(tmp_path: Path, page: dict[str, Any], **kw: Any) -> ops.OpContext:
    async def helper(name: str, **args: Any) -> Any:
        if name == "screenshot":
            assert sm.live(KEY) is None, "a held copy never goes through the helper's file"
        if name == "js":
            expr = args["expression"]
            for method, value in page.items():
                if f".{method}(" in expr:
                    return value
            return None
        if name == "page_info":
            return page.get("page_info")
        if name == "screenshot":
            Image.new("RGB", (200, 100), (255, 255, 255)).save(args["path"], format="PNG")
        return None

    return ops.OpContext(helper=helper, screenshot_dir=tmp_path, session_key=KEY, **kw)


@pytest.mark.asyncio
async def test_snapshot_console_and_wait_text_reach_the_agent_masked(tmp_path: Path) -> None:
    _hold()
    page = {
        "snapshot": {
            "url": "https://login.example.com/",
            "title": f"Welcome {EMAIL}",
            "nodes": [{"role": "heading", "name": f"Hi {EMAIL}", "ref": "e1", "depth": 0}],
        },
        "consoleMessages": [{"text": f"login ok {PASSWORD}"}],
    }
    ctx = _ctx(tmp_path, page)
    snap = await ops.run_op("snapshot", {}, ctx)
    logs = await ops.run_op("console", {}, ctx)
    text = json.dumps([snap, logs])
    assert snap["ok"] and PASSWORD not in text and EMAIL.lower() not in text.lower()
    assert "e1" in snap["snapshot"]  # refs and structure survive
    # The decision model and the fast loop read content through the same seam; the
    # guards' own reads (``_page``) are left whole, so a check never reads a mask.
    assert EMAIL not in json.dumps(await ops._read(ctx, "snapshot"))
    assert EMAIL in json.dumps(await ops._page(ctx, "snapshot"))


@pytest.mark.asyncio
async def test_a_screenshot_has_the_value_boxes_painted_black(tmp_path: Path) -> None:
    _hold()
    relay = _BoxRelay([[10, 10, 40, 20]], vw=100)  # the PNG is 200 wide: scale 2
    ctx = _ctx(tmp_path, {"askedValues": 0}, page_cdp=lambda: (relay, "S1"))
    out = await ops.run_op("screenshot", {}, ctx)
    assert out["ok"], out
    # Captured on the gateway's own session into memory: the helper wrote nothing,
    # so no raw pixels ever sat in the agent-readable directory.
    assert any(m == "Page.captureScreenshot" for m, _p, _s in relay.calls)
    with Image.open(out["path"]) as img:
        assert img.getpixel((40, 40)) == (0, 0, 0)  # inside (20..100, 20..60)
        assert img.getpixel((150, 90)) == (255, 255, 255)
    # The value went to the browser's own isolated world, lower-cased, never via the helper.
    assert any(m == "Page.createIsolatedWorld" for m, _p, _s in relay.calls)
    assert PASSWORD.lower() in relay.expressions[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["no_stream", "query_fails", "scrolled", "partial", "gone"])
async def test_a_screenshot_whose_value_boxes_are_unknown_goes_nowhere(
    tmp_path: Path, why: str
) -> None:
    _hold()
    relay = _BoxRelay([[10, 10, 40, 20]], vw=100)
    if why == "query_fails":
        relay.fail = True
    if why == "scrolled":
        relay.scrolls = [(0, 0), (0, 50)]
    if why == "partial":
        relay.partial = True
    if why == "gone":
        relay.gone = True
    cdp = None if why == "no_stream" else (relay, "S1")
    ctx = _ctx(tmp_path, {"askedValues": 0}, page_cdp=lambda: cdp)
    out = await ops.run_op("screenshot", {}, ctx)
    assert out["code"] == "asked_value_on_page"
    assert list(tmp_path.glob("shot-*.png")) == []


@pytest.mark.asyncio
async def test_with_no_copy_held_a_screenshot_is_taken_as_before(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"askedValues": 0})  # no stream needed
    assert (await ops.run_op("screenshot", {}, ctx))["ok"]


@pytest.mark.asyncio
async def test_the_decision_models_frame_is_masked_too(tmp_path: Path) -> None:
    from kiro_crew.browser_harness import decide

    _hold()
    relay = _BoxRelay([[0, 0, 100, 25]], vw=100)
    ctx = _ctx(tmp_path, {"askedValues": 0, "passwordBoxes": 0}, page_cdp=lambda: (relay, "S1"))
    data = await decide._capture(ctx)
    assert data is not None
    with Image.open(io.BytesIO(data)) as img:
        assert img.getpixel((5, 5)) == (0, 0, 0) and img.getpixel((5, 99)) == (255, 255, 255)
    relay.fail = True
    assert await decide._capture(ctx) is None


@pytest.mark.asyncio
async def test_the_other_origin_answer_drops_the_copy() -> None:
    _hold()
    relay = _BoxRelay([], vw=100)
    relay.gone = True
    # Unknown, not "nothing drawn": a frame captured before the navigation may show it.
    assert await sm.value_boxes(relay, "S1", KEY) is None and sm.live(KEY) is None


@pytest.mark.asyncio
async def test_an_ask_inside_a_frame_paints_the_frames_it_cannot_read() -> None:
    sm.hold(
        KEY,
        _ask(origin="https://idp.example.org", top="https://shop.example.com"),
        {"password": PASSWORD},
    )
    relay = _BoxRelay([], vw=100, frames=[[0, 50, 100, 50]])
    found = await sm.value_boxes(relay, "S1", KEY)
    assert found is not None and found.boxes == [[0.0, 50.0, 100.0, 50.0]]
    _hold()  # a top-frame ask: an unrelated third-party frame is left alone
    found = await sm.value_boxes(relay, "S1", KEY)
    assert found is not None and found.boxes == []


# ── the turn replay ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_replay_paints_the_value_boxes_and_the_card_does_not() -> None:
    stream, relay, sink = probe_stream(mask_passwords=True)
    sm.hold("chat-1", _ask(), {"password": PASSWORD})

    def evaluate() -> dict[str, Any]:
        params = relay.calls[-1][1]
        if "contextId" in params:  # the value query, in the isolated world
            return {
                "result": {
                    "value": {
                        "boxes": [[100, 100, 50, 20]],
                        "frames": [],
                        "partial": False,
                        "vw": 1280,
                        "sx": 0,
                        "sy": 0,
                    }
                }
            }
        return {"result": {"value": {"boxes": [[0, 0, 10, 10]], "frames": []}}}

    relay.answers["Runtime.evaluate"] = evaluate
    relay.answers["Page.getFrameTree"] = {"frameTree": {"frame": {"id": "F1"}}}
    relay.answers["Page.createIsolatedWorld"] = {"executionContextId": 7}
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    _w, _h, boxes, ok = sink.recorded[0]
    assert ok and [100, 100, 51, 21] in boxes
    assert sink.frames[0]["mask"] == [[0, 0, 11, 11]]  # the person's view: password boxes only
    relay.answers["Runtime.evaluate"] = lambda: (
        {"result": {"value": None}} if "contextId" in relay.calls[-1][1] else evaluate()
    )
    await relay.on_event(frame_event(jpeg((1, 1, 1), size=(1280, 800))))
    assert sink.recorded[-1][3] is False  # unknown value boxes: the frame is not kept


@pytest.mark.asyncio
async def test_the_replay_events_reach_disk_masked(tmp_path: Path) -> None:
    _hold()
    rec, _store, _clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    url = "https://login.example.com/u/octo.cat@example.com"
    rec.event(KEY, {"ts": 1, "op": "click", "actor": "agent", "url": url, "label": EMAIL})
    rec.turn_ended(KEY)
    await rec.flush()
    data = all_bytes(tmp_path).lower()
    assert b"octo.cat" not in data and b"click" in data


@pytest.mark.asyncio
async def test_wait_for_cannot_confirm_a_guess_while_a_copy_is_held(tmp_path: Path) -> None:
    _hold()
    page = {
        "textPresent": True,
        "observe": {"ok": True, "text": "Signed in as Octo.Cat@example.com"},
    }
    ctx = _ctx(tmp_path, page)
    guess = await ops.run_op("wait_for", {"text": "Signed in as Octo", "timeout": 0}, ctx)
    assert guess["ok"] is False and guess["code"] == "wait_timeout"
    plain = await ops.run_op("wait_for", {"text": "Signed in as", "timeout": 0}, ctx)
    assert plain["ok"] is True


def test_the_hub_drops_the_copy_when_the_tab_leaves_and_masks_replay_urls() -> None:
    from kiro_crew.browser_harness import live_hub as lh

    hub = lh.LiveHub()
    _hold()
    hub.page_changed(KEY, url="https://login.example.com/u/octo.cat@example.com")
    assert sm.live(KEY) is not None
    assert "octo.cat" not in hub.inner_mark(KEY).url.lower()
    hub.page_changed(KEY, url="https://elsewhere.example.net/")
    assert sm.live(KEY) is None


@pytest.mark.asyncio
async def test_the_replay_start_url_reaches_disk_masked(tmp_path: Path) -> None:
    _hold()
    rec, _store, _clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN, url="https://login.example.com/u/octo.cat@example.com")
    rec.note_url(KEY, "https://login.example.com/u/octo.cat@example.com/2")
    rec.turn_ended(KEY)
    await rec.flush()
    assert b"octo.cat" not in all_bytes(tmp_path).lower()


@pytest.mark.asyncio
async def test_the_isolated_world_is_reused_and_remade_after_a_navigation() -> None:
    _hold()
    relay = _BoxRelay([], vw=100)
    assert await sm.value_boxes(relay, "S1", KEY) is not None
    assert await sm.value_boxes(relay, "S1", KEY) is not None
    worlds = [c for c in relay.calls if c[0] == "Page.createIsolatedWorld"]
    assert len(worlds) == 1
    relay.stale_once = True  # the old context is gone
    assert await sm.value_boxes(relay, "S1", KEY) is not None
    assert len([c for c in relay.calls if c[0] == "Page.createIsolatedWorld"]) == 2
    relay.loader = "L2"  # a new document: a new world, without a failed query first
    assert await sm.value_boxes(relay, "S1", KEY) is not None
    assert len([c for c in relay.calls if c[0] == "Page.createIsolatedWorld"]) == 3


class _BoxRelay:
    """Answers the mask's three CDP calls with fixed boxes."""

    def __init__(self, boxes: list, *, vw: float, frames: list | None = None) -> None:
        self.boxes, self.vw, self.frames = boxes, vw, frames or []
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.expressions: list[str] = []
        self.fail = self.gone = self.partial = False
        self.scrolls: list[tuple[int, int]] = []
        self.stale_once = False
        self.loader = "L1"

    async def send(self, method: str, params: dict[str, Any], session_id: str) -> Any:
        self.calls.append((method, params, session_id))
        if self.fail:
            raise RuntimeError("boom")
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1", "loaderId": self.loader}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 9}
        if method == "Page.captureScreenshot":
            out = io.BytesIO()
            Image.new("RGB", (200, 100), (255, 255, 255)).save(out, format="PNG")
            return {"data": base64.b64encode(out.getvalue()).decode()}
        if self.stale_once:
            self.stale_once = False
            raise RuntimeError("Cannot find context with specified id")
        self.expressions.append(params["expression"])
        if self.gone:
            return {"result": {"value": {"gone": True}}}
        sx, sy = self.scrolls.pop(0) if self.scrolls else (0, 0)
        return {
            "result": {
                "value": {
                    "boxes": self.boxes,
                    "frames": self.frames,
                    "partial": self.partial,
                    "vw": self.vw,
                    "sx": sx,
                    "sy": sy,
                }
            }
        }
