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
(``credentials``) ask; never the plain ``text`` kind. A value shorter than
:data:`MIN_LEN` is matched only as a whole word, so ``bob`` masks "Hi bob" but not
"bobcat". The fields themselves stay withheld by element (``data-kc-asked``)
until the page navigates, held copy or not.

A copy is dropped when :data:`HOLD_S` runs out, when the tab's top frame is seen
on another origin, or when the session's live view stops; a second fill on the
same origin joins it. It is never logged, stored, published or put in an
exception.
"""

from __future__ import annotations

import asyncio
import base64
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
#: Shorter values are matched only as whole words.
MIN_LEN = 4
#: What a masked value reads as.
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
    """Keep a copy of the values *ask* is about to type, for :data:`HOLD_S`.

    A copy already held for the same origin is kept and joined (a sign-in, then
    its one-time code), so the username stays masked through the second step.
    """
    roles = _HELD_ROLES.get(ask.spec.kind, ())
    kept = tuple(v for r in roles if isinstance(v := values.get(r), str) and v.strip())
    if not kept:
        return
    origin = ask.top_origin or ask.origin
    framed = ask.origin != origin
    start = time.monotonic() if now is None else now
    before = live(session_key, now=start)
    if before is not None and before.origin == origin:
        kept = tuple(dict.fromkeys(before.values + kept))
        framed = framed or before.framed
    forms = sorted({f for v in kept for f in _variants(v)}, key=len, reverse=True)
    pattern = re.compile("|".join(_word(f) for f in forms), re.IGNORECASE)
    _HELD[session_key] = Held(kept, origin, framed, start + HOLD_S, pattern)


def _word(form: str) -> str:
    """A form as a pattern: anywhere when long enough, else only as a whole word."""
    escaped = re.escape(form)
    return escaped if len(form) >= MIN_LEN else rf"(?<!\w){escaped}(?!\w)"


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
    if isinstance(value, dict):
        # Only an op result's own top-level fields are the gateway's protocol; any
        # nested name (a caller-named read, a state) is masked like page text.
        return {k: v if k in _PROTOCOL_KEYS else _redact(held, v) for k, v in value.items()}
    return _redact(held, value)


#: An op result's top-level values the gateway itself writes, never page text.
_PROTOCOL_KEYS = frozenset({"code", "ref", "path", "target_id"})
#: The walker's shortest length cap (``snapshot_walker.js``): nothing shorter is cut.
_SHORTEST_CAP = 40
#: A cut string ends here when the walker marks the cut.
_ELLIPSIS = "\u2026"


def _redact(held: Held, value: Any) -> Any:
    if isinstance(value, str):
        return _cut_tail(held, held.pattern.sub(MASK, value))
    if isinstance(value, dict):
        # Keys are the walker's and the op's own protocol, never page text.
        return {k: _redact(held, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(held, v) for v in value]
    return value


def _cut_tail(held: Held, text: str) -> str:
    """Mask a value the walker's length cap cut short at the end of *text*.

    A cap keeps the start of a string, so a value (or an escaped form) can survive
    only as its own first characters at the very end of a string at least as long
    as the walker's smallest cap: masked when 3 or more remain, or any number
    before the walker's cut mark. A shorter string was never cut.
    """
    cut = text.endswith(_ELLIPSIS)
    body = text[:-1] if cut else text
    if len(body) < _SHORTEST_CAP:
        return text
    least = 1 if cut else 3
    low = body.lower()
    for lv in sorted({f.lower() for v in held.values for f in _variants(v)}, key=len):
        for n in range(min(len(lv) - 1, len(low)), least - 1, -1):
            if low.endswith(lv[:n]):
                return body[: len(body) - n] + MASK + text[len(body) :]
    return text


#: Where the values are drawn: text runs (by range) and input/textarea values (by
#: box), through open shadow roots and same-origin frames. ``frames`` are the boxes
#: of embedded documents it cannot read. Run in the top frame's isolated world, so
#: page script cannot see the values or move the boxes; the values are matched
#: lower-cased, and only when the document is still on the held origin.
_BOXES_JS = """(function (vals, origin, MIN) {
  if (location.origin !== origin) return { gone: true };
  var out = [], frames = [], partial = false;
  var push = function (r, ox, oy) {
    if (r.width > 0 && r.height > 0) out.push([r.left + ox, r.top + oy, r.width, r.height]);
  };
  // A short value counts only as a whole word (secret_mask.MIN_LEN).
  var word = /[0-9A-Za-z_\u00c0-\uffff]/;
  var whole = function (s, at, v) {
    return v.length >= MIN || ((at === 0 || !word.test(s.charAt(at - 1)))
      && (at + v.length >= s.length || !word.test(s.charAt(at + v.length))));
  };
  var hit = function (s) {
    s = s.toLowerCase();
    for (var i = 0; i < vals.length; i++) {
      for (var at = s.indexOf(vals[i]); at !== -1; at = s.indexOf(vals[i], at + 1)) {
        if (whole(s, at, vals[i])) return true;
      }
    }
    return false;
  };
  var walk = function (root, doc, d, ox, oy) {
    var tw = doc.createTreeWalker(root, 4), n;
    while ((n = tw.nextNode())) {
      var low = n.data.toLowerCase();
      for (var i = 0; i < vals.length; i++) {
        for (var at = low.indexOf(vals[i]); at !== -1; at = low.indexOf(vals[i], at + 1)) {
          if (!whole(low, at, vals[i])) continue;
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
#: The isolated world per CDP session (one tab each): the top document's loader
#: id it was made in, and its context id.
_WORLDS: dict[str, tuple[str, int]] = {}


async def _world(relay: Any, session_id: str) -> int:
    """The top document's isolated world, made again whenever the document changed."""
    tree = await relay.send("Page.getFrameTree", {}, session_id)
    frame = ((tree or {}).get("frameTree") or {}).get("frame") or {}
    loader = str(frame.get("loaderId") or "")
    cached = _WORLDS.get(session_id)
    if cached is not None and loader and cached[0] == loader:
        return cached[1]
    made = await relay.send(
        "Page.createIsolatedWorld",
        {"frameId": frame.get("id"), "worldName": _WORLD, "grantUniveralAccess": False},
        session_id,
    )
    ctx_id = made["executionContextId"]
    if not isinstance(ctx_id, int):
        raise TypeError("no context")
    _WORLDS[session_id] = (loader, ctx_id)
    return ctx_id


@dataclass(frozen=True)
class Boxes:
    #: Viewport CSS-pixel boxes to paint; the viewport width; the page's scroll.
    boxes: list[list[float]]
    vw: float
    scroll: tuple[float, float]


async def value_boxes(relay: Any, session_id: str, session_key: str) -> Boxes | None:
    """Where the session's held values are drawn on its tab now. ``None``: unknown.

    No copy held: no boxes. The top frame on another origin: the copy is dropped
    and this answer is ``None``: a frame captured just before that navigation can
    still show a value, so the new page's empty answer must not let it through. A
    frame it cannot read counts only for an ask that was itself inside a frame (its values live there): its whole box is painted.
    """
    held = live(session_key)
    if held is None:
        return Boxes([], 0.0, (0.0, 0.0))
    # The raw value and its URL-escaped forms, which a page can draw as link text.
    vals = json.dumps(
        sorted({f.lower() for v in held.values for f in (v, quote(v, safe=""), quote_plus(v))})
    )
    expr = f"{_BOXES_JS}({vals}, {json.dumps(held.origin)}, {MIN_LEN})"
    reply = None
    # One isolated world per document, reused (the replay asks up to 5 times a
    # second); a stale one is dropped and the query makes a fresh one once.
    for fresh in (False, True):
        try:
            if fresh:
                _WORLDS.pop(session_id, None)
            ctx_id = await _world(relay, session_id)
            reply = await relay.send(
                "Runtime.evaluate",
                {"expression": expr, "contextId": ctx_id, "returnByValue": True},
                session_id,
            )
            break
        except Exception:  # noqa: BLE001 - unknown, never "nothing drawn"; carries no value
            _WORLDS.pop(session_id, None)
            if fresh:
                logger.debug("secret mask: box query failed")
                return None
    value = ((reply or {}).get("result") or {}).get("value")
    if not isinstance(value, dict) or (reply or {}).get("exceptionDetails"):
        return None
    if value.get("gone"):
        drop(session_key)
        return None
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


def burn_png(data: bytes, found: Boxes, max_dim: int = 0) -> bytes | None:
    """The PNG with every box painted black, then fit in *max_dim*; ``None`` on failure."""
    if not found.boxes and not max_dim:
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
        if max_dim and max(rgb.size) > max_dim:
            rgb.thumbnail((max_dim, max_dim))
        out = io.BytesIO()
        rgb.save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 - an image we cannot mask is an image we do not send
        logger.debug("secret mask: burn failed")
        return None


async def masked_shot(ctx: Any, path: Any, *, max_dim: int = 0) -> bool:
    """Write the op's screenshot to *path* with every held value painted out.

    With no copy held this is the helper's own screenshot. With one held the frame
    is captured on the stream's own CDP session into memory, painted, and only the
    painted bytes are written: the raw pixels never touch a file the agent can
    read. ``False`` (nothing written) when the boxes are unknown (no stream on the
    tab, a failed query, the page left the origin), the page scrolled between the
    two box reads around the capture, or the paint failed.
    """
    key = getattr(ctx, "session_key", "")
    if not key or live(key) is None:
        extra = {"max_dim": max_dim} if max_dim else {}
        await ctx.helper("screenshot", path=str(path), **extra)
        return True
    cdp = ctx.page_cdp()
    if cdp is None:
        return False
    relay, session_id = cdp
    before = await value_boxes(relay, session_id, key)
    if before is None:
        return False
    try:
        shot = await relay.send("Page.captureScreenshot", {"format": "png"}, session_id)
        data = base64.b64decode((shot or {})["data"])
    except Exception:  # noqa: BLE001 - no frame, never an unmasked one
        logger.debug("secret mask: capture failed")
        return False
    after = await value_boxes(relay, session_id, key)
    if after is None:
        return False
    if (before.boxes or after.boxes) and after.scroll != before.scroll:
        return False  # the boxes would not be where the capture drew them
    both = Boxes(before.boxes + after.boxes, after.vw or before.vw, after.scroll)
    painted = await asyncio.to_thread(burn_png, data, both, max_dim)
    if painted is None:
        return False
    await asyncio.to_thread(path.write_bytes, painted)
    return True


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
