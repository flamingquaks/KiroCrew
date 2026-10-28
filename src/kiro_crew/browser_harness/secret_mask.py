"""Mask Secrets: a quick-ask value the gateway typed never comes back to the agent.

The person's values reach only the bound field (``quick_ask.py``). After the write
the page can still show them: a "Signed in as <email>" header, a password a
"show" toggle put in a text node, a title, a console line. This module keeps a
copy of the values the gateway just typed, in this process's memory only, and the
few exits that carry page content out use it:

* text: :func:`redact` replaces every occurrence (the value itself, any case, and
  its percent-, HTML- and JSON-escaped forms) in what a walker call, an op or the
  op route returns, which is all the agent and the decision model read;
* pixels: :func:`value_boxes` finds where the values are drawn, in the page's own
  isolated world, and :func:`burn_png` paints those boxes black in a screenshot
  for the agent or the decision model; the turn replay paints them in its frames
  (``screencast.py``). The person's own live view is left as the page draws it.

What is held: the password and the one-time code, and the username of a sign-in
(``credentials``) ask; never the plain ``text`` kind. Values shorter than
:data:`MIN_LEN` are not matched in text (they would hit ordinary words); the
fields themselves stay withheld by element (``data-kc-asked``) until the page
navigates, held copy or not.

A copy is dropped when :data:`HOLD_S` runs out, when a new ask is answered on the
session, or when the tab's top frame is seen on another origin. It is never
logged, stored, published or put in an exception.
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, quote_plus

logger = logging.getLogger(__name__)

#: How long a copy is kept after the gateway typed it.
HOLD_S = 5 * 60.0
#: Shortest value matched in text and pixels.
MIN_LEN = 4
#: What a masked value reads as. Shorter than :data:`MIN_LEN`, so it never holds one.
MASK = "***"
#: The roles whose values are held, by ask kind.
_HELD_ROLES = {"credentials": ("username", "password"), "otp": ("otp",)}
#: Most boxes one query may report; more and the mask is unknown.
BOX_LIMIT = 64


@dataclass(frozen=True)
class Held:
    values: tuple[str, ...]
    #: The tab's top-frame origin, and whether the ask was in a frame of another one.
    origin: str
    framed: bool
    until: float
    pattern: re.Pattern[str]


_HELD: dict[str, Held] = {}


def _variants(value: str) -> set[str]:
    return {
        value,
        quote(value, safe=""),
        quote_plus(value),
        html.escape(value),
        json.dumps(value)[1:-1],
    }


def hold(session_key: str, ask: Any, values: dict[str, str], *, now: float | None = None) -> None:
    """Keep a copy of the values *ask* is about to type, for :data:`HOLD_S`."""
    _HELD.pop(session_key, None)
    roles = _HELD_ROLES.get(ask.spec.kind, ())
    kept = tuple(v for r in roles if isinstance(v := values.get(r), str) and len(v) >= MIN_LEN)
    if not kept:
        return
    forms = sorted({f for v in kept for f in _variants(v)}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(f) for f in forms), re.IGNORECASE)
    origin = ask.top_origin or ask.origin
    start = time.monotonic() if now is None else now
    _HELD[session_key] = Held(kept, origin, ask.origin != origin, start + HOLD_S, pattern)


def drop(session_key: str) -> None:
    _HELD.pop(session_key, None)


def live(session_key: str, *, now: float | None = None) -> Held | None:
    """The session's copy, or ``None`` (none held, or it ran out and is dropped)."""
    held = _HELD.get(session_key)
    if held is None:
        return None
    if (time.monotonic() if now is None else now) >= held.until:
        _HELD.pop(session_key, None)
        return None
    return held


def seen_url(session_key: str, url: Any) -> None:
    """The tab's top frame is at *url*: a copy held for another origin is dropped."""
    held = _HELD.get(session_key)
    if held is None or not isinstance(url, str) or not url:
        return
    from kiro_crew.browser_harness.quick_ask import origin_of

    if origin_of(url) != held.origin:
        _HELD.pop(session_key, None)


def redact(session_key: str, value: Any) -> Any:
    """*value* with every held value in every string replaced by :data:`MASK`."""
    held = live(session_key) if session_key else None
    if held is None:
        return value
    return _redact(held.pattern, value)


def _redact(pattern: re.Pattern[str], value: Any) -> Any:
    if isinstance(value, str):
        return pattern.sub(MASK, value)
    if isinstance(value, dict):
        return {_redact(pattern, k): _redact(pattern, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(pattern, v) for v in value]
    return value


#: Where the values are drawn: text runs (by range) and input/textarea values (by
#: box), through open shadow roots and same-origin frames. ``frames`` are the boxes
#: of embedded documents it cannot read. Run in the top frame's isolated world, so
#: page script cannot see the values or move the boxes; the values are matched
#: lower-cased, and only when the document is still on the held origin.
_BOXES_JS = """(function (vals, origin) {
  if (location.origin !== origin) return { gone: true };
  var out = [], frames = [], partial = false;
  var push = function (r, ox, oy) {
    if (r.width > 0 && r.height > 0) out.push([r.left + ox, r.top + oy, r.width, r.height]);
  };
  var hit = function (s) {
    s = s.toLowerCase();
    for (var i = 0; i < vals.length; i++) if (s.indexOf(vals[i]) !== -1) return true;
    return false;
  };
  var walk = function (root, doc, d, ox, oy) {
    var tw = doc.createTreeWalker(root, 4), n;
    while ((n = tw.nextNode())) {
      var low = n.data.toLowerCase();
      for (var i = 0; i < vals.length; i++) {
        for (var at = low.indexOf(vals[i]); at !== -1; at = low.indexOf(vals[i], at + 1)) {
          var rg = doc.createRange();
          if (low.length === n.data.length) { rg.setStart(n, at); rg.setEnd(n, at + vals[i].length); }
          else rg.selectNodeContents(n);
          var rs = rg.getClientRects();
          for (var k = 0; k < rs.length; k++) push(rs[k], ox, oy);
        }
      }
    }
    var all = root.querySelectorAll('*');
    for (var j = 0; j < all.length; j++) {
      var el = all[j], tag = el.tagName;
      if ((tag === 'INPUT' || tag === 'TEXTAREA') && el.value && hit(el.value)) {
        push(el.getBoundingClientRect(), ox, oy);
      }
      var isF = tag === 'IFRAME' || tag === 'FRAME';
      if (!el.shadowRoot && !isF && tag !== 'OBJECT' && tag !== 'EMBED') continue;
      if (d >= 8) { partial = true; continue; }
      if (el.shadowRoot) walk(el.shadowRoot, doc, d + 1, ox, oy);
      if (!isF && tag !== 'OBJECT' && tag !== 'EMBED') continue;
      var b = el.getBoundingClientRect(), inner = null;
      if (b.width <= 0 || b.height <= 0) continue;
      if (isF) { try { inner = el.contentDocument; } catch (e) { inner = null; } }
      if (inner && inner.documentElement) {
        var cs = getComputedStyle(el);
        walk(inner.documentElement, inner, d + 1,
          ox + b.left + el.clientLeft + (parseFloat(cs.paddingLeft) || 0),
          oy + b.top + el.clientTop + (parseFloat(cs.paddingTop) || 0));
      } else frames.push([b.left + ox, b.top + oy, b.width, b.height]);
    }
  };
  walk(document.documentElement, document, 0, 0, 0);
  return { boxes: out, frames: frames, partial: partial, vw: innerWidth, sx: scrollX, sy: scrollY };
})"""

_WORLD = "kirocrew_secret_mask"


@dataclass(frozen=True)
class Boxes:
    #: Viewport CSS-pixel boxes to paint; the viewport width; the page's scroll.
    boxes: list[list[float]]
    vw: float
    scroll: tuple[float, float]


async def value_boxes(relay: Any, session_id: str, session_key: str) -> Boxes | None:
    """Where the session's held values are drawn on its tab now. ``None``: unknown.

    No copy held: no boxes. The top frame on another origin: the copy is dropped
    and there are no boxes. A frame it cannot read counts only for an ask that was
    itself inside a frame (its values live there): its whole box is painted.
    """
    held = live(session_key)
    if held is None:
        return Boxes([], 0.0, (0.0, 0.0))
    try:
        tree = await relay.send("Page.getFrameTree", {}, session_id)
        frame_id = ((tree or {}).get("frameTree") or {}).get("frame", {}).get("id")
        made = await relay.send(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "worldName": _WORLD, "grantUniveralAccess": False},
            session_id,
        )
        expr = f"{_BOXES_JS}({json.dumps([v.lower() for v in held.values])}, {json.dumps(held.origin)})"
        reply = await relay.send(
            "Runtime.evaluate",
            {"expression": expr, "contextId": made["executionContextId"], "returnByValue": True},
            session_id,
        )
    except Exception:  # noqa: BLE001 - unknown, never "nothing drawn"; the error carries no value
        logger.debug("secret mask: box query failed")
        return None
    value = ((reply or {}).get("result") or {}).get("value")
    if not isinstance(value, dict) or (reply or {}).get("exceptionDetails"):
        return None
    if value.get("gone"):
        drop(session_key)
        return Boxes([], 0.0, (0.0, 0.0))
    boxes, frames = value.get("boxes"), value.get("frames")
    if value.get("partial") or not isinstance(boxes, list) or not isinstance(frames, list):
        return None
    found = boxes + (frames if held.framed else [])
    if len(found) > BOX_LIMIT or not all(_box(b) for b in found):
        return None
    try:
        vw, sx, sy = float(value["vw"]), float(value["sx"]), float(value["sy"])
    except (KeyError, TypeError, ValueError):
        return None
    return Boxes([[float(v) for v in b] for b in found], vw, (sx, sy))


def _box(b: Any) -> bool:
    return (
        isinstance(b, list)
        and len(b) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1e7 for v in b)
    )


#: Pixels every box is grown by before it is painted (anti-aliased glyph edges).
PAD_PX = 3


def burn_png(data: bytes, found: Boxes) -> bytes | None:
    """The PNG with every box painted black; ``None`` when that cannot be done."""
    if not found.boxes:
        return data
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001 - no imaging library: a masked image cannot be made
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            rgb = img.convert("RGB")
        scale = rgb.width / found.vw if found.vw > 0 else 1.0
        draw = ImageDraw.Draw(rgb)
        for x, y, w, h in found.boxes:
            draw.rectangle(
                [
                    x * scale - PAD_PX,
                    y * scale - PAD_PX,
                    (x + w) * scale + PAD_PX,
                    (y + h) * scale + PAD_PX,
                ],
                fill=(0, 0, 0),
            )
        out = io.BytesIO()
        rgb.save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 - an image we cannot mask is an image we do not send
        logger.debug("secret mask: burn failed")
        return None


async def masked_shot(ctx: Any, path: Any, **kw: Any) -> bool:
    """Take the op's screenshot into *path* with every held value painted out.

    ``False`` when no image may go out: a copy is held and the boxes are unknown
    (no stream on the tab, a failed query), the page scrolled between the two box
    reads around the capture, or the paint failed. The file is then removed.
    """
    key = getattr(ctx, "session_key", "")
    if not key or live(key) is None:
        await ctx.helper("screenshot", path=str(path), **kw)
        return True
    cdp = ctx.page_cdp()
    if cdp is None:
        return False
    relay, session_id = cdp
    before = await value_boxes(relay, session_id, key)
    if before is None:
        return False
    await ctx.helper("screenshot", path=str(path), **kw)
    after = await value_boxes(relay, session_id, key)
    ok = after is not None
    if after is not None and (before.boxes or after.boxes):
        # Both reads saw the page at one scroll, so the boxes are where the capture drew.
        ok = after.scroll == before.scroll
        if ok:
            both = Boxes(before.boxes + after.boxes, after.vw or before.vw, after.scroll)
            ok = await asyncio.to_thread(_burn_file, path, both)
    if not ok:
        await asyncio.to_thread(_unlink, path)
    return ok


def _burn_file(path: Any, found: Boxes) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    painted = burn_png(data, found) if data else None
    if painted is None:
        return False
    path.write_bytes(painted)
    return True


def _unlink(path: Any) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "Boxes",
    "HOLD_S",
    "MASK",
    "MIN_LEN",
    "burn_png",
    "drop",
    "hold",
    "live",
    "masked_shot",
    "redact",
    "value_boxes",
]
