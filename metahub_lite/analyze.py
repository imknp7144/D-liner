"""
analyze.py
One-call entry point: image path in, labeled prompt sections out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .comfy_graph import Graph
from .comfy_resolve import PromptSection, resolve_sections
from .extract import extract


@dataclass
class AnalysisResult:
    """Richer sibling of plain analyze() (review point #12: "what was this
    Positive actually resolved FROM" was invisible to the UI -- only the
    final PromptSection list ever came out, with no trace of which source
    format won or how much confidence to put in it).

    Deliberately additive: analyze() keeps its original signature and
    behavior unchanged (it's now a two-line wrapper around this), so any
    existing caller -- including a future D-liner integration per HANDOFF
    02 §7-2 -- is unaffected. Only viewer.py is changed to call
    analyze_detailed() instead, to show the extra fields.
    """
    sections: list[PromptSection]
    source: str  # "workflow" | "prompt" | "a1111" | "none"
    warnings: list[str] = field(default_factory=list)


def analyze(path: str | Path) -> list[PromptSection]:
    return analyze_detailed(path).sections


def analyze_detailed(path: str | Path) -> AnalysisResult:
    result = extract(path)

    # (source_label, sections) pairs -- kept together so the winning
    # candidate's origin can still be reported after max()-by-score picks
    # it, instead of only ever returning the bare section list.
    candidates: list[tuple[str, list[PromptSection]]] = []
    if result.workflow:
        graph = Graph.from_workflow(result.workflow)
        candidates.append(("workflow", resolve_sections(graph)))
    if result.prompt:
        graph = Graph.from_prompt(result.prompt)
        candidates.append(("prompt", resolve_sections(graph)))

    # Bug fix (2026-08-15): resolve_sections() always returns a *list of
    # PromptSection objects* as long as it structurally found a pos/neg
    # slot, even when that slot's .text ends up None. `if sections:` was
    # therefore true (non-empty list) even for a completely empty Positive,
    # so the "workflow" branch below used to return immediately and never
    # even tried "prompt" -- silently locking in an empty result whenever
    # workflow-format structurally found the right nodes but couldn't
    # resolve their text, no matter how well prompt-format would have done.
    # Now every available format is resolved and scored, and the best one
    # wins -- "best" meaning most real resolved text, with unresolved-node
    # placeholders penalized so a format that mostly gives up doesn't win
    # on raw character count against a format that actually resolved less
    # text but all of it real.
    #
    # NOTE (2026-08-15): a "workflow" vs "prompt" tie-break preferring
    # "workflow" when scores are close was tried and reverted -- it fixed
    # one file where the two blocks had genuinely inconsistent embedded
    # data (see CHECKLIST.md), but broke another file where "prompt" was
    # correctly more complete than "workflow" at a similar score ratio
    # (AttentionCouple region decomposition succeeding in one format and
    # not the other). Raw content score has no way to distinguish "these
    # two blocks describe different generations" from "one format just
    # resolved a bit less of the same generation" -- that residual case is
    # a data-quality issue in the source file, not something this function
    # can resolve algorithmically without an oracle. THIS TIE-BREAK IS
    # DELIBERATELY NOT REINSTATED BELOW -- workflow vs prompt selection
    # stays pure max-by-score with no format preference, exactly as it was
    # left after that revert.
    #
    # Bug fix (regression fixtures 15/16, later session): a1111_text used
    # to be tried ONLY when every workflow/prompt candidate came back
    # completely empty (`if real_candidates:` gated on list emptiness, not
    # on whether the content was any good). That let a workflow/prompt
    # candidate that "structurally found something" but scored close to
    # nothing -- e.g. a near-empty workflow chunk holding a single
    # unrelated Note, or a completely unresolved placeholder chain -- beat
    # a fully complete, correct A1111 "parameters" block sitting right next
    # to it in the same file, with A1111 never even considered. A1111 is
    # now folded into the same scored candidate pool as workflow/prompt
    # instead of being a last-resort fallback, using the identical
    # _content_score() function -- not a new tie-break, just extending the
    # existing "most real resolved content wins" rule to a third source.
    # This does introduce one new, currently-hypothetical risk worth
    # flagging: if a file ever legitimately carries both a valid ComfyUI
    # graph AND a large *stale* A1111 "parameters" chunk left over from a
    # re-save, the stale block could now outscore a short-but-correct
    # graph resolution. No real file like that has been seen so far
    # (A1111 "parameters" and ComfyUI workflow/prompt chunks coexisting at
    # all is itself not something HANDOFF 02's 13 reference images ever
    # showed) -- flagged here so it's easy to find if one ever turns up.
    if result.a1111_text:
        candidates.append(("a1111", _parse_a1111(result.a1111_text)))

    real_candidates = [(label, c) for label, c in candidates if c]
    if not real_candidates:
        return AnalysisResult(sections=[], source="none")

    best_label, best_sections = max(real_candidates, key=lambda pair: _content_score(pair[1]))

    warnings = []
    placeholder_count = sum(
        (s.text or "").count(m) for s in best_sections for m in _UNRESOLVED_MARKERS
    )
    if placeholder_count:
        warnings.append(f"未対応/未解決の箇所が{placeholder_count}件あります")
    if len(real_candidates) > 1:
        other_labels = [label for label, _ in real_candidates if label != best_label]
        warnings.append(
            f"{SOURCE_DISPLAY_NAMES.get(best_label, best_label)}を採用しました"
            f"（{'/'.join(SOURCE_DISPLAY_NAMES.get(l, l) for l in other_labels)}も存在しますが"
            "スコアが下回ったため不採用）"
        )

    return AnalysisResult(sections=best_sections, source=best_label, warnings=warnings)


SOURCE_DISPLAY_NAMES = {
    "workflow": "ComfyUI Workflow",
    "prompt": "ComfyUI Prompt(実行グラフ)",
    "a1111": "A1111 parameters",
    "none": "なし",
}


_UNRESOLVED_MARKERS = ("[未対応ノード", "[プリセット参照")


def _content_score(sections: list[PromptSection]) -> int:
    score = 0
    for s in sections:
        if s.label == "Note":
            # Notes are documentation, not resolved prompt content -- a
            # workflow-format fallback sweep that grabbed a pile of Notes
            # (real, but irrelevant to the question "did we find the real
            # Positive/Negative") shouldn't be able to outscore a format
            # that correctly resolved less text but all of it the actual
            # prompt.
            continue
        text = s.text or ""
        if not text:
            continue
        placeholder_count = sum(text.count(m) for m in _UNRESOLVED_MARKERS)
        if placeholder_count:
            # A result that's mostly "couldn't resolve this" placeholders
            # is worse than a shorter-but-real result from the other
            # format -- don't let raw length make it win.
            score -= placeholder_count * 1000
        else:
            score += len(text)
    return score


def _parse_a1111(text: str) -> list[PromptSection]:
    """A1111's "parameters" text has its own explicit structure -- a
    "Negative prompt:" marker, then a final line of comma-separated
    settings (Steps/Sampler/CFG scale/...). Split on that marker rather
    than guessing, since the format already tells us where the boundary
    is.

    Made regex-based and case/whitespace-tolerant (review point #10):
    different A1111-family forks/frontends have been seen writing
    "negative prompt:" lowercase, "Negative Prompt :" with a space before
    the colon, and CRLF line endings. The original exact-substring match
    ("\\nNegative prompt:") missed all of those and fell back to treating
    the whole thing as one undifferentiated "A1111" block. This still
    requires the marker to start its own line (not just appear anywhere
    as a substring), which is what makes it safe against false positives
    inside an ordinary prompt that happens to mention "steps" in passing.
    """
    neg_match = _NEG_PROMPT_MARKER_RE.search(text)
    if neg_match is None:
        return [PromptSection("A1111", "parameters", text)]

    positive = text[:neg_match.start()].strip()
    rest = text[neg_match.end():]

    # The settings line is the first line starting with "Steps:" (A1111's
    # convention); everything before it is the negative prompt proper.
    settings_match = _STEPS_MARKER_RE.search(rest)
    if settings_match is None:
        negative, settings = rest.strip(), None
    else:
        negative = rest[:settings_match.start()].strip()
        settings = settings_match.group(1).strip()

    sections = [
        PromptSection("Positive", "parameters", positive),
        PromptSection("Negative", "parameters", negative),
    ]
    if settings:
        sections.append(PromptSection("Settings", "parameters", settings))
    return sections


# Both markers must start their own line (allowing for CRLF and incidental
# leading whitespace) -- this is what keeps the match from ever firing on
# a stray "steps" or "negative prompt" mentioned mid-sentence inside an
# ordinary prompt, which a plain case-insensitive substring search would
# risk doing.
_NEG_PROMPT_MARKER_RE = re.compile(r"\r?\n[ \t]*Negative prompt[ \t]*:[ \t]*", re.IGNORECASE)
_STEPS_MARKER_RE = re.compile(r"\r?\n[ \t]*(Steps[ \t]*:.*)", re.IGNORECASE | re.DOTALL)
