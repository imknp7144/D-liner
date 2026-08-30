"""
extract.py
Format-agnostic extraction of embedded generation metadata from image files.

Covers three real-world storage patterns confirmed during design/testing:
  1. PNG tEXt/iTXt chunks (standard ComfyUI / A1111 embedding)
  2. WebP / JPEG EXIF fields prefixed with "Workflow: " / "Prompt: "
     (seen in Nano-Banana / Gemini-style img2img saves)
  3. Plain A1111 "parameters" text (no JSON graph at all)

Returns a normalized ExtractResult regardless of source container, so the
resolver layer never has to care which file format it came from.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PIL import Image

try:
    import piexif
except ImportError:  # pragma: no cover
    piexif = None


@dataclass
class ExtractResult:
    workflow: Optional[dict] = None   # full UI graph (nodes/links/...), richest
    prompt: Optional[dict] = None     # execution graph ({node_id: {class_type, inputs, _meta}})
    a1111_text: Optional[str] = None  # raw A1111 "parameters" string, if that's all there is
    source: str = ""                  # where it came from, for debugging/UI display

    @property
    def has_graph(self) -> bool:
        return self.workflow is not None or self.prompt is not None

    @property
    def is_empty(self) -> bool:
        return not self.has_graph and not self.a1111_text


def extract(path: str | Path) -> ExtractResult:
    path = Path(path)
    suffix = path.suffix.lower()

    # Both _extract_png/_extract_exif read everything they need (img.text /
    # img.info) synchronously before returning, so it's safe to close the
    # file the moment extraction finishes rather than leaving the handle
    # open for the caller's lifetime (matters on Windows: a lingering
    # handle can block a subsequent move/delete of the same file from
    # D-liner).
    with Image.open(path) as img:
        if suffix == ".png":
            return _extract_png(img)
        if suffix in (".webp", ".jpg", ".jpeg"):
            return _extract_exif(img)

        # Unknown extension: try both strategies defensively.
        result = _extract_png(img)
        if not result.is_empty:
            return result
        return _extract_exif(img)


def _try_json(text: str) -> Optional[dict]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    # json.loads() happily returns non-dict JSON (a list, a bare number, a
    # string...) -- callers all treat the result as a node/graph dict
    # (Graph.from_workflow() calls workflow.get("nodes", []), etc.), so
    # anything else must be treated the same as "not parseable" rather than
    # propagating a type that crashes several hops downstream (regression
    # fixture 14_workflow_is_json_array.png: AttributeError before this fix).
    return value if isinstance(value, dict) else None


def _extract_png(img: Image.Image) -> ExtractResult:
    text_chunks: dict[str, str] = getattr(img, "text", {}) or {}

    workflow = _try_json(text_chunks["workflow"]) if "workflow" in text_chunks else None
    prompt = _try_json(text_chunks["prompt"]) if "prompt" in text_chunks else None
    a1111 = text_chunks.get("parameters")

    # Bug fix (regression fixtures 15/16): a1111_text used to be nulled out
    # whenever a "workflow" or "prompt" chunk was merely *present* in the
    # file, even if it was empty or unresolvable (a near-empty workflow
    # chunk is common -- ComfyUI writes one on most saves). That silently
    # threw away a perfectly good A1111 "parameters" block sitting right
    # next to it, with no way for analyze() to ever consider it. Always
    # keep a1111_text when present; analyze() (not extract()) is
    # responsible for deciding which of the available candidates to use.
    return ExtractResult(
        workflow=workflow,
        prompt=prompt,
        a1111_text=a1111,
        source="png_text_chunk",
    )


# EXIF tag ids we check, in priority order. Different savers pick different
# tags for the same purpose, so we scan all of them rather than assuming one.
# (Confirmed in practice: one saver used ImageDescription/Make, another used
# Make/Model for the same two payloads -- tag assignment isn't standardized.)
_EXIF_CANDIDATE_TAGS = {
    "0th": [270, 271, 272, 305, 315],  # ImageDescription, Make, Model, Software, Artist
    "Exif": [0x9286],             # UserComment
}


def _extract_exif(img: Image.Image) -> ExtractResult:
    if piexif is None or "exif" not in img.info:
        return ExtractResult(source="exif_unavailable")

    try:
        exif_dict = piexif.load(img.info["exif"])
    except Exception:
        return ExtractResult(source="exif_parse_failed")

    workflow = None
    prompt = None
    a1111 = None

    for ifd_name, tags in _EXIF_CANDIDATE_TAGS.items():
        ifd = exif_dict.get(ifd_name, {})
        for tag in tags:
            raw = ifd.get(tag)
            if not raw:
                continue
            text = _decode_exif_value(raw)
            if text is None:
                continue

            # Prefix format varies by saver: "Workflow: {...}" (capitalized,
            # space after colon) and "workflow:{...}" (lowercase, no space)
            # have both been seen in practice for the same payload kind.
            # Match case-insensitively and locate the JSON start explicitly
            # rather than assuming an exact prefix string/length.
            lowered = text.lstrip().lower()
            brace_idx = text.find("{")

            if lowered.startswith("workflow") and brace_idx != -1:
                workflow = _try_json(text[brace_idx:]) or workflow
            elif lowered.startswith("prompt") and brace_idx != -1:
                prompt = _try_json(text[brace_idx:]) or prompt
            elif text.lstrip().startswith("{"):
                # Unlabeled JSON blob: guess by shape (workflow has "nodes",
                # prompt is a flat dict of numeric-looking keys -> class_type).
                parsed = _try_json(text)
                if isinstance(parsed, dict):
                    if "nodes" in parsed:
                        workflow = workflow or parsed
                    elif any(isinstance(v, dict) and "class_type" in v for v in parsed.values()):
                        prompt = prompt or parsed
            elif _looks_like_a1111(text):
                a1111 = a1111 or text

    # Same fix as _extract_png: don't discard a1111_text just because a
    # workflow/prompt tag also happened to be present.
    return ExtractResult(
        workflow=workflow,
        prompt=prompt,
        a1111_text=a1111,
        source="exif_tag",
    )


# Case/whitespace-tolerant A1111 detection (review point #10): different
# forks/frontends have been seen writing "steps:" lowercase and with a
# space before the colon. This only gates *whether* an unlabeled EXIF blob
# is treated as A1111 text at all -- the actual Positive/Negative/Settings
# split (analyze._parse_a1111) has its own, separately tolerant matching.
_A1111_DETECT_RE = re.compile(r"(?:steps|negative prompt)[ \t]*:", re.IGNORECASE)


def _looks_like_a1111(text: str) -> bool:
    return bool(_A1111_DETECT_RE.search(text))


def _decode_exif_value(raw: Any) -> Optional[str]:
    if isinstance(raw, bytes):
        # EXIF UserComment has an 8-byte charset prefix (e.g. b"ASCII\x00\x00\x00")
        if raw[:8] in (b"ASCII\x00\x00\x00",):
            raw = raw[8:]
        try:
            return raw.decode("utf-8", errors="ignore")
        except Exception:
            return None
    if isinstance(raw, str):
        return raw
    return None
