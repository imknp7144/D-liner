"""
comfy_resolve.py
Resolves human-readable prompt sections out of a ComfyUI graph. Plain link-
tracing (follow "text" -> CLIPTextEncode) only works for the simplest
workflows; real ones route prompt text through several different bundling/
indirection conventions before it reaches the sampler. This module handles
each one (all validated against real akaaka workflows):

  1. "easy" node suite PIPE_LINE bundles (easy pipeIn/pipeOut/kSampler/
     preSampling*/cleanGpuUsed) -> unwrapped by field name (pos/neg/model/
     ...), not by node class name. See _resolve_pipe_field.
  2. rgthree/easy "setNode"/"getNode" virtual links, matched by widget name
     string -- these are NOT present in the links array at all. See
     _setnode_index / _resolve_via_setnode.
  3. ComfyRoll ("CR") pipe bundles (CR Module Pipe Loader/Input/Output) --
     a structurally different bundle convention from "easy"'s, with three
     distinct node roles instead of two. See _resolve_cr_pipe_field.
  4. Native ComfyUI samplers (KSampler*, SamplerCustom, FaceDetailer) that
     wire positive/negative/model directly -- no pipe/bundle at all -- and
     guider-based ones (SamplerCustomAdvanced -> CFGGuider) that need one
     extra hop through the guider node first. See
     _resolve_sections_from_sampler's direct-wire/guider branches.
  5. Dual-conditioning passthrough nodes (ControlNetApplyAdvanced,
     InpaintModelConditioning) that re-emit a modified positive+negative
     pair -- "positive" must keep following "positive", not fall over to
     whichever single input a node happens to expose. See
     _resolve_conditioning_source.
  6. Multi-output "pipe unwrapper" nodes (CR Module Input, easy pipeOut)
     that expose several differently-named outputs (model/pos/neg/vae/...)
     from one input -- which output a downstream link used determines
     which field is being asked for, which requires tracking the source
     node's output *slot index*, not just its identity. See
     Graph.get_input_with_slot / output_name_at_slot and
     _resolve_multi_output_conditioning_field.
  7. Nodes with real literal content whose downstream ShowText|pysssss
     display cache is more trustworthy than reconstructing it structurally
     (a sub-branch can resolve to a stale cached value from a different
     run than the one this display node captured). See _get_display_cache
     / _CACHE_FIRST_TYPES.
  8. Regional conditioning (AttentionCouplePPM/AttentionCouple) hanging off
     the MODEL wire -- base_cond/cond_1/cond_2 are separate prompts, not
     one canonical positive string, and are further decomposed when they
     share a common sub-source (see _resolve_conditioning_parts).

Falls back to a flat "gather every text-bearing node" pass for graphs with no
sampler at all (e.g. Note-only editing workflows).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .comfy_graph import Graph, Node

# Recursion depth safety valves. These exist purely to stop a malformed or
# maliciously-crafted cyclic graph from recursing forever -- real workflows
# never come close. Two separate limits because the call sites have very
# different natural depths: general graph/pipe/conditioning traversal
# (_resolve_text, _resolve_pipe_field, _resolve_model_chain,
# _resolve_cr_pipe_field, _resolve_conditioning_source) can legitimately
# walk through many chained utility/passthrough nodes, while
# _resolve_conditioning_parts only ever unwraps a conditioning-combiner
# chain (BREAK segments), which is shallow by construction.
MAX_GRAPH_DEPTH = 60
MAX_CONDITIONING_PARTS_DEPTH = 20

PIPE_BASED_SAMPLER_TYPES = {"easy kSampler", "easy kSamplerCustom", "easy kSamplerTiled"}
DIRECT_WIRE_SAMPLER_TYPES = {
    "KSampler", "KSamplerAdvanced", "SamplerCustom",
    # Impact Pack's face-refinement node: its own direct model/positive/
    # negative inputs make it structurally identical to a native sampler
    # for resolution purposes. Recognizing it matters specifically when a
    # queued execution only re-ran a downstream detail pass on an already-
    # cached base image -- the "prompt" (execution graph) format then
    # contains no primary KSampler at all, only this.
    "FaceDetailer",
}
# SamplerCustomAdvanced doesn't carry positive/negative/model itself -- it
# takes a "guider" input instead, which encapsulates them (CFGGuider and
# friends). One extra hop, otherwise the same direct-wire shape.
GUIDER_BASED_SAMPLER_TYPES = {"SamplerCustomAdvanced"}
GUIDER_TYPES_WITH_DIRECT_COND = {"CFGGuider", "ScheduledCFGGuider //Inspire"}
SAMPLER_TYPES = PIPE_BASED_SAMPLER_TYPES | DIRECT_WIRE_SAMPLER_TYPES | GUIDER_BASED_SAMPLER_TYPES

# Node types _resolve_pipe_field knows how to unwrap further -- when an
# "easy pipeIn" node's direct field-override link lands on one of these
# (rather than a genuine terminal prompt-bearing node), that link must be
# followed through _resolve_pipe_field recursively, not returned as-is.
_PIPE_INDIRECTION_TYPES = (
    PIPE_BASED_SAMPLER_TYPES | SAMPLER_TYPES |
    {"easy pipeIn", "easy pipeOut", "easy setNode", "easy getNode", "Reroute",
     "ReroutePrimitive|pysssss",
     "easy preSampling", "easy preSamplingAdvanced", "easy preSamplingCustom"}
)
# Reroute-family nodes: pure single-input passthrough, but the input name
# varies by variant. Plain "Reroute" uses an unnamed ("") input; pysssss's
# primitive-aware ReroutePrimitive uses "value" instead.
_REROUTE_INPUT_NAMES = {"Reroute": "", "ReroutePrimitive|pysssss": "value"}
COUPLING_TYPES = {"AttentionCouplePPM", "AttentionCouple"}
TEXT_CONTAINER_TYPES = {
    "CLIPTextEncode", "CLIPTextEncodeBREAK", "TextMultilineSwitchable",
    "Text Concatenate", "ShowText|pysssss", "Note",
}


# ---------------------------------------------------------------------------
# Section data model
# ---------------------------------------------------------------------------

@dataclass
class PromptSection:
    label: str        # "Base", "Regional A", "Positive", "Note", ...
    title: str         # the node's own title in ComfyUI, if any
    text: Optional[str]


_CACHE_FIRST_TYPES = {
    # Pure passthrough/combination nodes: their own value is entirely
    # *derived* from other nodes, so a downstream display cache and a
    # structural reconstruction should represent the same thing -- but
    # reconstruction can silently combine a fresh sub-branch with a stale
    # cached one two levels deeper (see bug #18), where the cache attached
    # to *this* node is closer to what the sampler actually consumed and
    # is therefore more trustworthy. Node types with real literal content
    # of their own (CLIPTextEncode's widget, TextMultilineSwitchable's own
    # non-external text) are deliberately NOT here -- for those, a fresh
    # edit to the node's own value should win over an older cache.
    "Text Concatenate", "ConditioningConcat", "Reroute", "ReroutePrimitive|pysssss", "easy setNode", "easy getNode",
}


# ---------------------------------------------------------------------------
# Core text resolution: _resolve_text walks CONDITIONING/STRING wires by
# node identity (no "which field" concept -- see section 6/8 below for where
# that's needed and how it's bridged in). _resolve_text_body holds the
# per-node-type dispatch; _get_display_cache is the ShowText fallback (7).
# ---------------------------------------------------------------------------

def _resolve_text(graph: Graph, node: Optional[Node], visited: set, depth: int = 0) -> Optional[str]:
    if node is None or depth > MAX_GRAPH_DEPTH:
        return None
    key = node.id
    if key in visited:
        # Already on the CURRENT recursion path -- a true cycle (A's
        # resolution depends on A itself, possibly via several hops). Not
        # the same thing as "already resolved somewhere earlier in this
        # call tree": `visited` tracks the active call stack, not global
        # history, and is popped in the `finally` below before returning
        # to the caller. This matters for legitimate diamond shapes where
        # two different branches both legitimately reference the same
        # upstream node (e.g. a shared framing CLIPTextEncode feeding two
        # separate Text Concatenate branches) -- see regression fixture
        # 09_shared_node_diamond.json. A global "ever visited" set (the
        # previous behavior) silently dropped the second branch's content;
        # this recursion-stack version only blocks genuine self-reference.
        return None
    visited.add(key)
    try:
        if node.class_type in _CACHE_FIRST_TYPES:
            cached = _get_display_cache(graph).get(node.id)
            if cached:
                return cached

        result = _resolve_text_body(graph, node, visited, depth)

        if _is_unresolved(result):
            cached = _get_display_cache(graph).get(node.id)
            if cached:
                return cached

        return result
    finally:
        visited.discard(key)


def _is_unresolved(text: Optional[str]) -> bool:
    return text is None or text.startswith("[未対応ノード") or text.startswith("[プリセット参照")


_DISPLAY_CACHE_TYPES_EXPLICIT = {"ShowText|pysssss"}


def _looks_like_text_display_node(class_type: str) -> bool:
    """Heuristic for community text-preview/display nodes we don't know by
    exact class name (e.g. future pysssss/rgthree-style additions): a
    'Show'/'Preview'/'Display' node whose class name also mentions text."""
    lowered = class_type.lower()
    return "text" in lowered and any(k in lowered for k in ("show", "preview", "display"))


def _get_showtext_cached_value(node: Node) -> Optional[str]:
    """ShowText|pysssss stores widgets_values[0] as a *list* of strings (one
    per queued run in a batch), not a bare string -- graph.get_widget()'s
    plain index lookup would hand back that inner list. Shared by the
    display-cache index and this node type's own direct resolution."""
    wv = node.widgets_values
    if not wv:
        return None
    raw = wv[0]
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    return raw if isinstance(raw, str) and raw else None


def _get_display_cache(graph: Graph) -> dict[str, str]:
    """Maps upstream_node_id -> text that a downstream 'ShowText'-style
    display node cached for it at generation time.

    ComfyUI's frontend writes a node's live execution result back into that
    node's own widgets_values, and that snapshot is exactly what gets
    embedded in the saved image's workflow JSON. So a display node sitting
    downstream of an otherwise-unresolvable dynamic source (LLM captioning,
    a PresetText reference, etc.) is often the *only* place the actual
    generated text for that specific run survives -- even when the display
    node itself has no consumers and looks like a dead end structurally.

    Only meaningful for "workflow" format graphs (widgets_values doesn't
    exist in "prompt" format); returns {} there. Built once per graph and
    memoized on the Graph instance since multiple _resolve_text calls
    consult it.
    """
    cached = getattr(graph, "_display_cache", None)
    if cached is not None:
        return cached

    index: dict[str, str] = {}
    if graph.format == "workflow":
        for node in graph.nodes.values():
            t = node.class_type
            if t not in _DISPLAY_CACHE_TYPES_EXPLICIT and not _looks_like_text_display_node(t):
                continue
            raw = _get_showtext_cached_value(node)
            if not raw:
                continue
            kind, src = graph.get_input(node, "text")
            if kind == "node" and src is not None:
                index[src.id] = raw

    graph._display_cache = index
    return index


def _resolve_text_body(graph: Graph, node: Node, visited: set, depth: int) -> Optional[str]:
    t = node.class_type

    # Bypassed/muted nodes must not have their own stored value trusted --
    # ComfyUI treats them as transparent, forwarding whatever live input
    # feeds their "passthrough" socket instead. If no such link exists,
    # a bypassed node contributes nothing (not its stale widget content).
    if not graph.is_active(node) and t in ("CLIPTextEncode", "CLIPTextEncodeBREAK", "smZ CLIPTextEncode", "TextMultilineSwitchable", "ShowText|pysssss"):
        passthrough_field = "external_text" if t == "TextMultilineSwitchable" else "text"
        kind, val = graph.get_input(node, passthrough_field)
        if kind == "node":
            return _resolve_text(graph, val, visited, depth + 1)
        if kind == "literal":
            return val
        return None  # bypassed with nothing live feeding it: no value, not a stale one

    if t == "ImpactWildcardEncode":
        # widgets_values[0]/[1] (workflow format) are the raw template and
        # "populated_text" (post wildcard/LoRA-tag expansion); in prompt
        # format the same two values arrive as literal "wildcard_text"/
        # "populated_text" inputs instead -- get_widget() only resolves
        # for workflow format. populated_text is authoritative either way:
        # wildcards resolve randomly each run, so it's "what really got
        # used", not the template.
        kind, populated = graph.get_input(node, "populated_text")
        if kind == "literal" and populated:
            return populated
        populated = graph.get_widget(node, 1)
        if populated:
            return populated
        kind, template = graph.get_input(node, "wildcard_text")
        if kind == "literal" and template:
            return template
        return graph.get_widget(node, 0)

    if t in ("CLIPTextEncode", "CLIPTextEncodeBREAK", "smZ CLIPTextEncode"):
        # smZNodes' "smZ CLIPTextEncode" is a drop-in CLIPTextEncode
        # alternative (A1111-style prompt weighting) with the same text
        # widget/input shape -- same handling applies.
        kind, val = graph.get_input(node, "text")
        v = None
        if kind == "node":
            v = _resolve_text(graph, val, visited, depth + 1)
            # A linked source that itself couldn't resolve (e.g. a
            # PresetText|pysssss reference, or a dynamic node) returns an
            # unresolved-placeholder string -- which is still truthy, so
            # blindly trusting "non-empty" here would prefer that
            # placeholder over this node's own perfectly good literal
            # widget value. Only trust the link if it resolved to real text.
            if v and not _is_unresolved(v):
                return v
        elif kind == "literal":
            return val
        widget_val = graph.get_widget(node, 0)
        if widget_val:
            return widget_val
        # Nothing better available: surface whatever the link gave us
        # (even a placeholder/None) rather than silently returning nothing.
        return v

    if t == "TextMultilineSwitchable":
        # graph.get_widget() only resolves in "workflow" format -- in
        # "prompt" (execution-graph) format these same values arrive as
        # ordinary literal inputs instead, so try get_input() first and
        # fall back to get_widget() for workflow-format graphs where the
        # widget was never promoted to a named input.
        kind, val = graph.get_input(node, "use_external")
        use_external = val if kind == "literal" else graph.get_widget(node, 1)
        if use_external:
            kind, val = graph.get_input(node, "external_text")
            if kind == "node":
                return _resolve_text(graph, val, visited, depth + 1)
            if kind == "literal":
                return val
            return None
        kind, val = graph.get_input(node, "text")
        if kind == "literal":
            return val
        if kind == "node":
            return _resolve_text(graph, val, visited, depth + 1)
        return graph.get_widget(node, 0)

    if t == "Text Concatenate":
        kind, val = graph.get_input(node, "delimiter")
        delimiter = val if (kind == "literal" and val) else (graph.get_widget(node, 0) or ", ")
        parts = []
        for letter in "abcd":
            kind, val = graph.get_input(node, f"text_{letter}")
            if kind == "node":
                v = _resolve_text(graph, val, visited, depth + 1)
                if v:
                    parts.append(v)
            elif kind == "literal" and val:
                parts.append(str(val))
        return delimiter.join(parts) if parts else None

    if t in ("ShowText|pysssss",):
        kind, val = graph.get_input(node, "text")
        if kind == "node":
            return _resolve_text(graph, val, visited, depth + 1)
        if kind == "literal":
            return val
        return _get_showtext_cached_value(node)

    if t == "Note":
        return graph.get_widget(node, 0)

    if t == "StringFunction|pysssss":
        # pysssss's general string utility node. Only "append" (plain
        # concatenation) is handled -- other actions ("replace", "remove",
        # regex-based ones) transform the text in ways that would need
        # actually running the logic to reproduce faithfully, which is out
        # of scope here; those fall through to the generic "unresolved"
        # placeholder rather than silently returning something wrong.
        kind_action, action = graph.get_input(node, "action")
        if kind_action != "literal":
            action = graph.get_widget(node, 0)
        kind_a, val_a = graph.get_input(node, "text_a")
        kind_b, val_b = graph.get_input(node, "text_b")
        if action == "append":
            parts = []
            for kind, val in ((kind_a, val_a), (kind_b, val_b)):
                if kind == "node":
                    v = _resolve_text(graph, val, visited, depth + 1)
                    if v:
                        parts.append(v)
                elif kind == "literal" and val:
                    parts.append(str(val))
            return "".join(parts) if parts else None
        return None

    if t == "StringConcatenate":
        # Native ComfyUI core utility node: string_a, string_b, delimiter.
        # Unlike StringFunction|pysssss's "append" mode, the delimiter is
        # an explicit third widget/input here, not always empty.
        parts = []
        for field, widget_idx in (("string_a", 0), ("string_b", 1)):
            kind, val = graph.get_input(node, field)
            if kind == "node":
                v = _resolve_text(graph, val, visited, depth + 1)
                if v:
                    parts.append(v)
            elif kind == "literal" and val:
                parts.append(str(val))
            else:
                widget_val = graph.get_widget(node, widget_idx)
                if widget_val:
                    parts.append(str(widget_val))
        kind, delimiter = graph.get_input(node, "delimiter")
        if kind != "literal":
            delimiter = graph.get_widget(node, 2)
        delimiter = delimiter if isinstance(delimiter, str) else ""
        return delimiter.join(parts) if parts else None

    if t in ("easy positive", "easy negative"):
        # easy suite's standalone literal text holder nodes -- widget-only,
        # no inputs of their own, plain text widget at index 0.
        kind, val = graph.get_input(node, t.split(" ")[1])  # "positive" / "negative"
        if kind == "literal":
            return val
        return graph.get_widget(node, 0)

    if t in _REROUTE_INPUT_NAMES:
        kind, val = graph.get_input(node, _REROUTE_INPUT_NAMES[t])
        if kind == "node":
            return _resolve_text(graph, val, visited, depth + 1)
        return None

    if t == "easy getNode":
        return _resolve_via_setnode(graph, node, lambda n, v: _resolve_text(graph, n, v, depth + 1), visited)

    if t == "ConditioningConcat":
        # This is what "BREAK" compiles down to: two CLIPTextEncode-family
        # segments encoded separately, then concatenated at the CONDITIONING
        # level (not the text level) to work around the 77-token CLIP limit.
        parts = []
        for field in ("conditioning_to", "conditioning_from"):
            kind, val = graph.get_input(node, field)
            if kind == "node":
                v = _resolve_text(graph, val, visited, depth + 1)
                if v:
                    parts.append(v)
        return " BREAK ".join(parts) if parts else None

    if t == "ImpactConcatConditionings":
        # Impact Pack's N-way version of the same idea: "conditioning1",
        # "conditioning2", ... (declared count varies per instance, unused
        # trailing slots are just left unconnected rather than omitted).
        parts = []
        for i in range(1, 9):
            kind, val = graph.get_input(node, f"conditioning{i}")
            if kind == "node":
                v = _resolve_text(graph, val, visited, depth + 1)
                if v:
                    parts.append(v)
            elif kind == "literal" and val:
                parts.append(str(val))
        return " BREAK ".join(parts) if parts else None

    if t == "ImpactSwitch":
        # Impact Pack's manual multi-input switch: widgets_values[0] is the
        # 1-based selected index in workflow format; in prompt format the
        # same value arrives as an ordinary literal "select" input instead
        # (graph.get_widget() only resolves for workflow format).
        kind, select = graph.get_input(node, "select")
        if kind != "literal":
            select = graph.get_widget(node, 0)
        if isinstance(select, (int, float)):
            kind, val = graph.get_input(node, f"input{int(select)}")
            if kind == "node":
                return _resolve_text(graph, val, visited, depth + 1)
            if kind == "literal":
                return val
        return None

    # Generic conditioning-modifier passthrough: nodes like
    # ConditioningSetTimestepRange / ConditioningSetArea / ConditioningCombine
    # take an upstream "conditioning" (singular) and re-emit a modified
    # CONDITIONING without touching the text. Rather than hardcoding every
    # such node's class name, follow any input literally called
    # "conditioning" -- if present, it's almost always this kind of node.
    #
    # The source might land on a multi-output field-sensitive node (a CR
    # Module Input's 9 outputs, or "easy fullLoader"'s separate positive/
    # negative) rather than a genuine single-purpose passthrough -- slot
    # info is needed to know which field this specific link carries.
    kind, val, slot = graph.get_input_with_slot(node, "conditioning")
    if kind == "node":
        if val.class_type in _CR_UNWRAPPER_TYPES or val.class_type in _STRING_INPUT_CONDITIONING_TYPES:
            resolved = _resolve_multi_output_conditioning_field(graph, val, slot, depth + 1)
            if resolved is not None:
                return _resolve_text(graph, resolved, visited, depth + 1)
        return _resolve_text(graph, val, visited, depth + 1)

    if t == "PresetText|pysssss":
        # This node doesn't hold literal text at all -- it stores a
        # *preset name* that resolves against an external preset library
        # file (pysssss extension), which lives outside the graph and is
        # never embedded in the image. The name itself is still useful
        # context, so surface it plainly rather than a generic "unknown".
        name = graph.get_widget(node, 0)
        return f"[プリセット参照: 「{name}」 — 実テキストは画像のメタデータに含まれず、ComfyUI側のプリセットファイルにあります]"

    # Unknown node type reached as a text source (e.g. an API/LLM captioning
    # node whose output only exists at execution time and isn't stored as a
    # widget value in a static workflow export). Say so explicitly rather
    # than silently returning nothing -- a labeled "can't resolve" beats a
    # blank section that looks like "no prompt was used here".
    return f"[未対応ノード ({t}) — 動的生成の可能性があり、静的なworkflow JSONからは復元できません]"


# ---------------------------------------------------------------------------
# "easy" node suite: rgthree-style setNode/getNode virtual links (2) and
# PIPE_LINE bundle unwrapping by field name (1).
# ---------------------------------------------------------------------------

def _setnode_index(graph: Graph) -> dict[str, Node]:
    """Maps setNode widget name -> the "easy setNode" node registered under
    it. Built once per graph and memoized on the Graph instance (same
    pattern as _get_display_cache) since every getNode resolution across
    the whole graph calls this -- rebuilding it by scanning all nodes on
    every single call made large workflows do a full node-list scan once
    per getNode hop instead of once total."""
    cached = getattr(graph, "_setnode_index_cache", None)
    if cached is not None:
        return cached

    index: dict[str, Node] = {}
    for n in graph.all_nodes_by_class("easy setNode"):
        name = graph.get_widget(n, 0)
        if name:
            index[name] = n
    graph._setnode_index_cache = index
    return index


def _resolve_via_setnode(graph: Graph, getnode: Node, recurse, visited: set):
    name = graph.get_widget(getnode, 0)
    setnode = _setnode_index(graph).get(name)
    if setnode is None:
        return None
    if not graph.is_active(setnode):
        # A bypassed setNode is a dead/alternate branch's wiring, not a
        # live value -- do not follow it just because it's the only node
        # registered under this name. Better to report "unresolved" than
        # to confidently surface stale content from a disabled branch.
        return None
    for _name, src in graph.iter_connected_inputs(setnode):
        return recurse(src, visited)
    return None


def _resolve_pipe_field(graph: Graph, node: Optional[Node], field: str, depth: int = 0) -> Optional[Node]:
    if node is None or depth > MAX_GRAPH_DEPTH:
        return None
    t = node.class_type

    if t == "easy pipeIn":
        # A direct field link (e.g. "pos") takes priority over the fallback
        # "pipe" bundle -- but only if it's actually live. Graph editing in
        # ComfyUI easily leaves a stale direct override wired to a since-
        # bypassed node while the "pipe" input was rewired to the real,
        # active source; blindly trusting whichever link exists picks the
        # dead one. Prefer an active direct link, fall back to the pipe
        # chain, and only resort to a bypassed direct link if nothing else
        # resolves at all (still better context than an empty section).
        #
        # If the direct link lands on ANOTHER indirection node (getNode/
        # setNode/Reroute/pipeIn/a sampler-ish pipe passthrough) rather
        # than a terminal prompt-bearing node, that indirection must be
        # followed too -- a getNode can itself point at a bypassed setNode
        # and resolve to nothing, which should be treated exactly like "the
        # direct link was dead" (fall through to the pipe chain), not
        # returned as an unresolved getNode node itself.
        kind, val = graph.get_input(node, field)
        direct_result = None
        if kind == "node" and graph.is_active(val):
            if val.class_type in _PIPE_INDIRECTION_TYPES:
                direct_result = _resolve_pipe_field(graph, val, field, depth + 1)
            else:
                direct_result = val
        if direct_result is not None:
            return direct_result

        kind2, pipe_val = graph.get_input(node, "pipe")
        if kind2 == "node":
            result = _resolve_pipe_field(graph, pipe_val, field, depth + 1)
            if result is not None:
                return result

        if kind == "node":
            return val
        return None

    if t in ("easy pipeOut", *SAMPLER_TYPES, "easy preSampling", "easy preSamplingAdvanced", "easy preSamplingCustom"):
        kind, val = graph.get_input(node, "pipe")
        if kind == "node":
            return _resolve_pipe_field(graph, val, field, depth + 1)
        return None

    if t == "easy cleanGpuUsed":
        # Pure passthrough utility node (VRAM cleanup side-effect; forwards
        # whatever's wired into "anything" unchanged) -- generically named
        # input, not "pipe", so it needs its own branch rather than falling
        # into the generic PIPE_LINE fallback below (which only checks a
        # literal "pipe"-named input).
        kind, val = graph.get_input(node, "anything")
        if kind == "node":
            return _resolve_pipe_field(graph, val, field, depth + 1)
        return None

    if t == "easy setNode":
        for _name, src in graph.iter_connected_inputs(node):
            return _resolve_pipe_field(graph, src, field, depth + 1)
        return None

    if t == "easy getNode":
        name = graph.get_widget(node, 0)
        setnode = _setnode_index(graph).get(name)
        if setnode and graph.is_active(setnode):
            for _name, src in graph.iter_connected_inputs(setnode):
                return _resolve_pipe_field(graph, src, field, depth + 1)
        return None

    if t in _REROUTE_INPUT_NAMES:
        kind, val = graph.get_input(node, _REROUTE_INPUT_NAMES[t])
        if kind == "node":
            return _resolve_pipe_field(graph, val, field, depth + 1)
        return None

    if t == "easy fullLoader":
        # This all-in-one loader node is itself effectively a pipe bundle
        # *origin* for positive/negative purposes -- its own "positive"/
        # "negative" STRING inputs (distinct from its same-named
        # CONDITIONING *outputs*) are what actually feed the pipe's pos/neg
        # slots when a chain routes through it. _resolve_pipe_field uses
        # "pos"/"neg" naming (the "easy" pipe bundle's own convention);
        # this node's inputs use "positive"/"negative" instead.
        string_field = {"pos": "positive", "neg": "negative"}.get(field)
        if string_field:
            kind, val = graph.get_input(node, string_field)
            if kind == "node":
                return val
        return None

    # Generic PIPE_LINE passthrough: any node we don't specifically know
    # about (CFG/sampler-tweak utility nodes, custom pipe-editing nodes,
    # etc.) that has its own "pipe" input is almost certainly just
    # re-emitting a modified pipe, not originating one. Follow it rather
    # than giving up -- this covers nodes like "enhancedDynamicCFGSettings"
    # without having to name every such wrapper explicitly.
    kind, val = graph.get_input(node, "pipe")
    if kind == "node":
        return _resolve_pipe_field(graph, val, field, depth + 1)

    return None


# ---------------------------------------------------------------------------
# AttentionCouple regional-conditioning support (8): walks the MODEL wire
# looking for a coupling node, since base_cond/cond_1/cond_2/cond_3 aren't
# reachable from the sampler's own "positive" input at all.
# ---------------------------------------------------------------------------

def _resolve_model_chain(graph: Graph, node: Optional[Node], depth: int = 0) -> Optional[Node]:
    if node is None or depth > MAX_GRAPH_DEPTH:
        return None
    t = node.class_type

    if t in COUPLING_TYPES:
        if graph.is_active(node):
            return node
        # Bypassed coupling node: ComfyUI passes MODEL straight through,
        # so this is not a real regional-conditioning split for this run --
        # keep walking its own "model" input instead of stopping here.

    kind, val = graph.get_input(node, "model")
    if kind == "node":
        return _resolve_model_chain(graph, val, depth + 1)

    if t in _REROUTE_INPUT_NAMES:
        kind, val = graph.get_input(node, _REROUTE_INPUT_NAMES[t])
        if kind == "node":
            return _resolve_model_chain(graph, val, depth + 1)
        return None

    if t == "easy getNode":
        name = graph.get_widget(node, 0)
        setnode = _setnode_index(graph).get(name)
        if setnode and graph.is_active(setnode):
            for _name, src in graph.iter_connected_inputs(setnode):
                return _resolve_model_chain(graph, src, depth + 1)
        return None

    if t == "easy setNode":
        for _name, src in graph.iter_connected_inputs(node):
            return _resolve_model_chain(graph, src, depth + 1)
        return None

    if t == "Any Switch (rgthree)":
        for name, src in graph.iter_connected_inputs(node):
            if name.startswith("any_"):
                return _resolve_model_chain(graph, src, depth + 1)
        return None

    return None


# ---------------------------------------------------------------------------
# ComfyRoll ("CR") pipe bundles (3), dual-conditioning passthrough (5), and
# the slot-aware bridge between them and multi-output unwrappers (6). This
# is the densest part of the file -- these three concerns interleave because
# a chain can hop between ecosystems (native sampler -> ControlNetApply* ->
# CR Module Input -> CR Module Pipe Loader, as one real example needed).
# ---------------------------------------------------------------------------

_DUAL_CONDITIONING_PASSTHROUGH_TYPES = {
    # Nodes that take a positive+negative CONDITIONING pair and re-emit a
    # modified pair without touching the underlying text (ControlNet
    # application, inpaint-mask conditioning, ...). When a native sampler's
    # "positive"/"negative" input lands on one of these, the real prompt
    # source is further upstream on that SAME field -- "positive" must keep
    # following "positive", not fall over to whichever single input a node
    # happens to expose (unlike ConditioningSetTimestepRange etc., which
    # only have one slot and are already handled generically in
    # _resolve_text via the "conditioning" input name).
    "ControlNetApplyAdvanced", "ControlNetApply", "InpaintModelConditioning",
}


_CR_UNWRAPPER_TYPES = {"CR Module Input", "CR Module Output"}

_EASY_PIPE_UNWRAPPER_TYPES = {"easy pipeOut"}

_STRING_INPUT_CONDITIONING_TYPES = {"easy fullLoader"}


_CR_CHAIN_TYPES = {"CR Module Pipe Loader", "CR Module Input", "CR Module Output", "Reroute", "ReroutePrimitive|pysssss"}


def _continue_cr_field(graph: Graph, val: Optional[Node], field: str, depth: int) -> Optional[Node]:
    """A CR node's direct field-override link can point at another CR
    chain node (e.g. "CR Module Output".pos wired straight to a "CR
    Module Input" unwrapper's own "pos" output), OR at a dual-conditioning
    passthrough node from an entirely different family (ControlNetApply*,
    InpaintModelConditioning -- see _DUAL_CONDITIONING_PASSTHROUGH_TYPES)
    rather than a terminal prompt-bearing node. Keep unwrapping through
    whichever applies; otherwise `val` already is the terminal node and
    should be handed back as-is."""
    if val is None:
        return None
    if val.class_type in _CR_CHAIN_TYPES:
        return _resolve_cr_pipe_field(graph, val, field, depth)
    if val.class_type in _DUAL_CONDITIONING_PASSTHROUGH_TYPES:
        # `field` here is CR's own "pos"/"neg" naming; _resolve_conditioning_source
        # expects the sampler-level "positive"/"negative" convention instead.
        cond_field = "positive" if field == "pos" else "negative"
        return _resolve_conditioning_source(graph, val, cond_field, depth)
    return val


def _resolve_cr_pipe_field(graph: Graph, node: Optional[Node], field: str, depth: int = 0) -> Optional[Node]:
    """Walks a ComfyRoll ("CR") pipe-bundle chain looking for the node that
    actually supplies `field` ("pos" or "neg", CR's own naming). Structurally
    the same job _resolve_pipe_field does for the "easy" node suite's pipe
    system, kept separate because CR has (at least) three distinct node
    roles rather than "easy"'s two:
      - "CR Module Pipe Loader": pure constructor, all fields as direct
        named inputs -> one "pipe" output.
      - "CR Module Output": re-bundler -- base "pipe" input PLUS optional
        direct field-override inputs -> a new "pipe" output. Same dual-role
        shape as "easy pipeIn" (override wins if present and live,
        otherwise fall through to the base pipe).
      - "CR Module Input": pure unwrapper -- one "pipe" input -> several
        read-only named outputs (model/pos/neg/...), no overrides of its
        own despite the confusingly similar name to the above.
    """
    if node is None or depth > MAX_GRAPH_DEPTH:
        return None
    t = node.class_type

    if t == "CR Module Pipe Loader":
        kind, val = graph.get_input(node, field)
        return _continue_cr_field(graph, val, field, depth + 1) if kind == "node" else None

    if t == "CR Module Output":
        kind, val = graph.get_input(node, field)
        if kind == "node" and graph.is_active(val):
            return _continue_cr_field(graph, val, field, depth + 1)
        kind2, pipe_val = graph.get_input(node, "pipe")
        if kind2 == "node":
            result = _resolve_cr_pipe_field(graph, pipe_val, field, depth + 1)
            if result is not None:
                return result
        return _continue_cr_field(graph, val, field, depth + 1) if kind == "node" else None

    if t == "CR Module Input":
        kind, val = graph.get_input(node, "pipe")
        if kind == "node":
            return _resolve_cr_pipe_field(graph, val, field, depth + 1)
        return None

    if t in _REROUTE_INPUT_NAMES:
        kind, val = graph.get_input(node, _REROUTE_INPUT_NAMES[t])
        if kind == "node":
            return _resolve_cr_pipe_field(graph, val, field, depth + 1)
        return None

    return None


def _resolve_multi_output_conditioning_field(graph: Graph, node: Node, slot: Optional[int], depth: int = 0) -> Optional[Node]:
    """Given a node reached via a *generic* single "conditioning" link
    (e.g. from ConditioningSetTimestepRange's passthrough) that turns out
    to actually be a multi-output field-sensitive node -- a CR Module
    Input's 9 outputs, or "easy fullLoader"'s separate positive/negative --
    resolves to the real text-bearing node for the SPECIFIC field the
    caller's link came from, using the output slot index to recover which
    field that is. Returns None if `node` isn't such a type, the slot
    can't be identified, or that field isn't resolvable.

    This exists because _resolve_text has no "which field" concept (it
    just walks CONDITIONING wires by node identity), while
    _resolve_cr_pipe_field/_resolve_pipe_field/_STRING_INPUT_CONDITIONING_TYPES
    handling all need one -- this is the bridge between those conventions.
    """
    if node.class_type in _CR_UNWRAPPER_TYPES:
        cr_field = graph.output_name_at_slot(node, slot)
        if cr_field in ("pos", "neg"):
            return _resolve_cr_pipe_field(graph, node, cr_field, depth)
        return None

    if node.class_type in _EASY_PIPE_UNWRAPPER_TYPES:
        # "easy pipeOut": the "easy" node suite's own multi-output pipe
        # unwrapper (model/pos/neg/latent/vae/clip/image/seed from one
        # "pipe" input) -- structurally the same role as CR Module Input,
        # different ecosystem. Its own output naming ("pos"/"neg") already
        # matches _resolve_pipe_field's convention, so no translation needed.
        easy_field = graph.output_name_at_slot(node, slot)
        if easy_field in ("pos", "neg"):
            return _resolve_pipe_field(graph, node, easy_field, depth)
        return None

    if node.class_type in _STRING_INPUT_CONDITIONING_TYPES:
        field = graph.output_name_at_slot(node, slot)
        if field in ("positive", "negative"):
            kind, val = graph.get_input(node, field)
            return val if kind == "node" else None
        return None

    return None


def _resolve_conditioning_input(graph: Graph, node: Node, input_name: str, field: str, depth: int = 0) -> Optional[Node]:
    """Slot-aware entry point for resolving a node's own positive/negative
    CONDITIONING input (a native sampler, or a guider node reached via
    "guider") -- unlike intermediate hops through a dual-conditioning
    passthrough (ControlNetApply* etc.), this very first hop can itself
    land directly on a multi-output unwrapper (easy pipeOut, CR Module
    Input), so it needs the same slot-aware dispatch, not just a plain
    get_input()."""
    kind, val, slot = graph.get_input_with_slot(node, input_name)
    if kind != "node":
        return None
    resolved = _resolve_multi_output_conditioning_field(graph, val, slot, depth + 1)
    if resolved is not None:
        return resolved
    return _resolve_conditioning_source(graph, val, field, depth + 1)


def _resolve_conditioning_source(graph: Graph, node: Optional[Node], field: str, depth: int = 0) -> Optional[Node]:
    """Given a node feeding a native sampler's 'positive' or 'negative'
    input directly (no pipe bundle), walks back through any dual-
    conditioning passthrough nodes to find the actual prompt-bearing node.
    `field` is 'positive' or 'negative' and picks which of a passthrough
    node's own two conditioning inputs to keep following.

    Also unwraps ComfyRoll's "CR Module Input" pattern: a 9-output
    unwrapper (model/pos/neg/vae/clip/...) fed by one "pipe" input.
    Which output a downstream link used matters here -- get_input() alone
    can't tell us that (every node type this resolver handled until now had
    exactly one relevant output), so this uses get_input_with_slot() and
    the node's own declared output name at that slot to recover which CR
    field ("pos"/"neg") is actually being asked for, then hands off to
    _resolve_cr_pipe_field to walk the rest of that bundle's ancestry.
    """
    if node is None or depth > MAX_GRAPH_DEPTH:
        return node
    t = node.class_type

    if t in _DUAL_CONDITIONING_PASSTHROUGH_TYPES:
        kind, val, slot = graph.get_input_with_slot(node, field)
        if kind == "node":
            resolved = _resolve_multi_output_conditioning_field(graph, val, slot, depth + 1)
            if resolved is not None:
                return resolved
            return _resolve_conditioning_source(graph, val, field, depth + 1)
        return node

    if t in _CR_UNWRAPPER_TYPES:
        # Reached directly (not via a dual-passthrough hop above) -- we
        # don't know which of its 9 outputs got us here without slot info
        # from the *caller's* link, which by definition we don't have at
        # this entry point. Best effort: map the sampler-side field name to
        # CR's naming and try that.
        cr_field = "pos" if field == "positive" else "neg"
        result = _resolve_cr_pipe_field(graph, node, cr_field, depth + 1)
        return result if result is not None else node

    if t in _EASY_PIPE_UNWRAPPER_TYPES:
        # Same "reached directly, no slot info" situation as CR Module
        # Input above, for the "easy" suite's own unwrapper instead.
        easy_field = "pos" if field == "positive" else "neg"
        result = _resolve_pipe_field(graph, node, easy_field, depth + 1)
        return result if result is not None else node

    if t in _STRING_INPUT_CONDITIONING_TYPES:
        # "easy fullLoader" (and similar all-in-one loader nodes from the
        # easy suite) bundle CheckpointLoader+CLIPTextEncode-equivalent
        # positive/negative encoding into one node. Its "positive"/
        # "negative" CONDITIONING *outputs* are internally derived from
        # same-named STRING *inputs* (which is what an external getNode/
        # "easy negative"/etc. actually connects to when overriding the
        # prompt) -- a completely different pair of sockets from the
        # CONDITIONING outputs we arrived here via. Whatever's wired to
        # that STRING input is handed straight to _resolve_text (which
        # already knows how to follow getNode/setNode/CLIPTextEncode/
        # "easy negative"/etc. from here).
        kind, val = graph.get_input(node, field)
        if kind == "node":
            return val
        # No live override wired to the STRING input: the loader's own
        # widget default would be the fallback, but widgets_values has no
        # embedded field names and converting a widget to an input
        # reorders the visible inputs list, so a positional guess isn't
        # safe for this node type without its Python source. Report
        # unresolved rather than guessing wrong.
        return node

    if t in _REROUTE_INPUT_NAMES:
        kind, val = graph.get_input(node, _REROUTE_INPUT_NAMES[t])
        if kind == "node":
            return _resolve_conditioning_source(graph, val, field, depth + 1)
        return node

    return node


# ---------------------------------------------------------------------------
# Top-level entry points: given an active sampler node, build its Positive/
# Negative/Regional PromptSections (4); given a whole graph, find the best
# sampler(s) to start from and fall back to a flat text sweep if none
# resolve to anything (used for e.g. Note-only editing workflows).
# ---------------------------------------------------------------------------

def _resolve_sections_from_sampler(graph: Graph, ks: Node) -> list[PromptSection]:
    """Builds prompt sections starting from one specific active sampler node.
    Returns [] if this sampler's chain doesn't resolve to anything."""
    sections: list[PromptSection] = []

    if ks.class_type in PIPE_BASED_SAMPLER_TYPES:
        kind, pipe_src = graph.get_input(ks, "pipe")
        if kind != "node":
            return []
        pos_node = _resolve_pipe_field(graph, pipe_src, "pos")
        neg_node = _resolve_pipe_field(graph, pipe_src, "neg")
        model_node_start = _resolve_pipe_field(graph, pipe_src, "model")
    elif ks.class_type in GUIDER_BASED_SAMPLER_TYPES:
        kind, guider = graph.get_input(ks, "guider")
        if kind != "node" or guider.class_type not in GUIDER_TYPES_WITH_DIRECT_COND:
            return []  # unrecognized guider type: don't guess, just skip
        pos_node = _resolve_conditioning_input(graph, guider, "positive", "positive")
        neg_node = _resolve_conditioning_input(graph, guider, "negative", "negative")
        kind, model_node_start = graph.get_input(guider, "model")
        if kind != "node":
            model_node_start = None
    else:
        # Native ComfyUI samplers (KSampler/KSamplerAdvanced/SamplerCustom)
        # and FaceDetailer wire positive/negative/model directly onto the
        # sampler node -- there's no "easy"-style pipe bundle involved at
        # all, so there's no bundle to unwrap; just follow each input
        # directly (through any multi-output unwrapper or dual-conditioning
        # passthrough node in the way -- slot-aware from the first hop,
        # since that first hop can itself land on a multi-output unwrapper
        # like "easy pipeOut" or "CR Module Input" rather than a single-
        # purpose node).
        pos_node = _resolve_conditioning_input(graph, ks, "positive", "positive")
        neg_node = _resolve_conditioning_input(graph, ks, "negative", "negative")
        kind, model_node_start = graph.get_input(ks, "model")
        if kind != "node":
            model_node_start = None

    coupling = _resolve_model_chain(graph, model_node_start)

    if coupling:
        fields = [("base_cond", "Base"), ("cond_1", "Regional A"),
                  ("cond_2", "Regional B"), ("cond_3", "Regional C")]

        field_parts: list[tuple[str, Node, list[tuple[str, str]]]] = []
        for field, default_label in fields:
            kind, src = graph.get_input(coupling, field)
            if kind == "node":
                field_parts.append((default_label, src, _resolve_conditioning_parts(graph, src)))

        # A source node reused across 2+ regions' concat chains (see
        # _resolve_conditioning_parts) gets factored out into one shared
        # "Base" section instead of appearing duplicated, verbatim, in
        # every region that includes it.
        key_counts: dict[str, int] = {}
        for _, _src, parts in field_parts:
            for key, _text in parts:
                key_counts[key] = key_counts.get(key, 0) + 1
        shared_key = next((k for k, c in key_counts.items() if c > 1), None)

        if shared_key is not None:
            shared_text = next(text for _, _src, parts in field_parts for k, text in parts if k == shared_key)
            sections.append(PromptSection("Base", "共通", shared_text))
            region_letter = ord("A")
            for _default_label, src, parts in field_parts:
                remaining = [text for k, text in parts if k != shared_key]
                if not remaining:
                    continue
                label = f"Regional {chr(region_letter)}"
                region_letter += 1
                sections.append(PromptSection(label, src.title or src.class_type, " BREAK ".join(remaining)))
        else:
            for default_label, src, parts in field_parts:
                text = " BREAK ".join(text for _, text in parts) if parts else None
                sections.append(PromptSection(default_label, src.title or src.class_type, text))
    elif pos_node is not None:
        visited = set()
        text = _resolve_text(graph, pos_node, visited)
        sections.append(PromptSection("Positive", pos_node.title or pos_node.class_type, text))

    if neg_node is not None:
        visited = set()
        text = _resolve_text(graph, neg_node, visited)
        sections.append(PromptSection("Negative", neg_node.title or neg_node.class_type, text))

    return sections


def _resolve_conditioning_parts(graph: Graph, node: Optional[Node], depth: int = 0) -> list[tuple[str, str]]:
    """Like _resolve_text, but for a conditioning-combiner node (BREAK's
    ConditioningConcat, or Impact Pack's N-way ImpactConcatConditionings),
    returns each combined part separately as (source_node_id, resolved_text)
    pairs instead of joining them into one string.

    Used by AttentionCouple region-building to detect when the *same*
    source node is reused across multiple regions -- a common "framing"
    text concatenated with each character's own specific text, e.g.
    base_cond = shared_framing + char_A, cond_1 = shared_framing + char_B.
    A flat per-field resolution would show that shared text duplicated
    verbatim in every region; comparing by node identity (not text) lets it
    be factored out and shown once instead. Non-combiner nodes resolve to a
    single one-part list.
    """
    if node is None or depth > MAX_CONDITIONING_PARTS_DEPTH:
        return []
    t = node.class_type

    if t == "ConditioningConcat":
        parts: list[tuple[str, str]] = []
        for field in ("conditioning_to", "conditioning_from"):
            kind, val = graph.get_input(node, field)
            if kind == "node":
                parts.extend(_resolve_conditioning_parts(graph, val, depth + 1))
        return parts

    if t == "ImpactConcatConditionings":
        parts = []
        for i in range(1, 9):
            kind, val = graph.get_input(node, f"conditioning{i}")
            if kind == "node":
                parts.extend(_resolve_conditioning_parts(graph, val, depth + 1))
        return parts

    text = _resolve_text(graph, node, set())
    return [(node.id, text)] if text else []


def _has_resolved_primary_text(sections: list[PromptSection]) -> bool:
    """True if the sections list contains an actually-resolved Positive/Base/
    Regional text (not None, not empty). Used to tell a 'this sampler's pipe
    chain genuinely produced text' result apart from a 'structurally found a
    pos/neg slot but it resolved to nothing' result -- the latter is what
    happens when resolve_sections walks a dead parallel pipe branch."""
    for s in sections:
        if s.label in ("Positive", "Base", "Regional A", "Regional B", "Regional C") and s.text:
            return True
    return False


def _is_consumed_by_active_sampler(graph: Graph, node: Node) -> bool:
    """True if some OTHER active sampler node has an input wired directly
    to this node's output (any slot) -- i.e. this sampler's own output
    (typically LATENT) feeds a later sampler stage, as in a hires-fix /
    second-pass setup where KSampler A's output is refined by KSampler B.
    Used by _score_sampler_candidate to tell an upstream/intermediate
    sampler apart from a genuinely final one (review point #1's more
    general form: "first resolved" isn't the same question as "which one
    is actually final"). O(active samplers x graph size); only called
    once per active sampler at the top of resolve_sections, not from deep
    recursion, so this is not a hot path the way _setnode_index was."""
    for other in graph.nodes.values():
        if other.id == node.id:
            continue
        if other.class_type not in SAMPLER_TYPES or not graph.is_active(other):
            continue
        for _input_name, src in graph.iter_connected_inputs(other):
            if src.id == node.id:
                return True
    return False


def _sampler_candidate_score(graph: Graph, ks: Node, sections: list[PromptSection]) -> tuple[int, int, int]:
    """Ranks one active sampler's resolved sections against another's.
    Returned as a tuple compared lexicographically (higher wins):

      1. is this sampler NOT consumed by another active sampler (i.e. is
         it the final stage, not an intermediate first-pass)
      2. did it resolve any real Positive/Base/Regional text at all
      3. total resolved, non-placeholder character count (Note sections
         excluded, same spirit as analyze._content_score)

    This replaces "return the first active sampler whose chain resolves
    real text" -- that rule picked correctly whenever exactly one sampler
    resolved anything, but had no way to choose between several that all
    resolved something (order in the node list decided it, arbitrarily),
    and no signal at all for "which sampler's output is the one that
    actually reached the final image" (regression fixtures 02a/02b/03)."""
    final_stage = 0 if _is_consumed_by_active_sampler(graph, ks) else 1
    resolved_primary = 1 if _has_resolved_primary_text(sections) else 0
    content_len = 0
    for s in sections:
        if s.label == "Note":
            continue
        text = s.text or ""
        if not text or "[未対応ノード" in text or "[プリセット参照" in text:
            continue
        content_len += len(text)
    return (final_stage, resolved_primary, content_len)


def resolve_sections(graph: Graph) -> list[PromptSection]:
    """Top-level entry point: returns labeled prompt sections for a graph.

    When a graph has more than one active sampler (parallel pipe systems --
    e.g. a legacy bypassed path left in place alongside a live one, or a
    multi-stage hires-fix pipeline), each active sampler's chain is resolved
    and scored (see _sampler_candidate_score), and the highest-scoring one
    wins. This replaces the earlier "first active sampler whose chain
    resolves real text wins" rule, which was order-dependent whenever more
    than one sampler resolved something, and had no way to prefer a final
    output stage over an upstream/intermediate one. If every candidate
    still scores as "resolved nothing", the highest-scoring one is returned
    anyway (better than nothing, same spirit as the old best_partial), and
    only if there were no active samplers at all does this fall back to the
    flat text sweep."""
    active_samplers = [
        n for n in graph.nodes.values()
        if n.class_type in SAMPLER_TYPES and graph.is_active(n)
    ]

    if not active_samplers:
        return _fallback_text_sweep(graph)

    candidates: list[tuple[tuple[int, int, int], list[PromptSection]]] = []
    for ks in active_samplers:
        sections = _resolve_sections_from_sampler(graph, ks)
        if sections:
            candidates.append((_sampler_candidate_score(graph, ks, sections), sections))

    if candidates:
        # Stable sort: on an exact score tie (e.g. two structurally
        # identical parallel branches with no distinguishing signal at
        # all), the first one in node order is kept, same as before --
        # there's no oracle to break a genuine tie with.
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]

    return _fallback_text_sweep(graph)


# ---------------------------------------------------------------------------
# Last-resort fallback: no sampler resolved anything (or none exists), so
# just gather every text-bearing node in the graph with a best-effort
# positive/negative keyword guess.
# ---------------------------------------------------------------------------

# Keyword sets used only as a last-resort heuristic in the fallback sweep,
# to at least group likely positive/negative blocks together when the graph
# gives us no structural pos/neg signal to go on. Deliberately NOT used for
# A1111 metadata, which already carries an explicit "Negative prompt:"
# marker -- guessing there would only make a reliable split less reliable.
_POSITIVE_MARKERS = (
    "masterpiece", "best quality", "high quality", "highres", "absurdres",
    "ultra-detailed", "ultra detailed", "amazing quality", "very aesthetic",
)
_NEGATIVE_MARKERS = (
    "worst quality", "low quality", "bad anatomy", "bad hands",
    "extra fingers", "watermark", "blurry", "jpeg artifacts", "signature",
    "username", "error",
)


def _guess_polarity(text: str) -> Optional[str]:
    lowered = text.lower()
    pos_hits = sum(1 for m in _POSITIVE_MARKERS if m in lowered)
    neg_hits = sum(1 for m in _NEGATIVE_MARKERS if m in lowered)
    if neg_hits >= 2 and neg_hits > pos_hits:
        return "Negative(推定)"
    if pos_hits >= 2 and pos_hits > neg_hits:
        return "Positive(推定)"
    return None


def _fallback_text_sweep(graph: Graph) -> list[PromptSection]:
    """No sampler found (e.g. Note-only editing workflow): surface every
    text-bearing node as-is, labeled by its own title. Sections that look
    like a positive/negative quality-tag block by keyword heuristic are
    promoted to the front so the person doesn't have to scroll past
    unrelated Note cards to find them."""
    sections = []
    # Exact-text dedupe only: several TEXT_CONTAINER_TYPES nodes commonly
    # resolve to the byte-for-byte same string (a shared upstream node
    # reached from multiple sweep entry points, or a plain duplicate node
    # left in the graph). Two *different* texts are always kept even if
    # one looks like a near-duplicate of the other -- only an identical
    # match is safe to drop without risking losing real content.
    seen_texts: set[str] = set()
    for node in graph.nodes.values():
        if node.class_type not in TEXT_CONTAINER_TYPES:
            continue
        if not graph.is_active(node):
            continue
        visited: set = set()
        text = _resolve_text(graph, node, visited)
        if text and text not in seen_texts:
            seen_texts.add(text)
            guess = _guess_polarity(text)
            label = guess if guess else node.class_type
            sections.append((0 if guess else 1, PromptSection(label, node.title or node.class_type, text)))

    sections.sort(key=lambda pair: pair[0])
    return [s for _, s in sections]
