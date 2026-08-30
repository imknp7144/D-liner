"""
comfy_graph.py
Normalizes ComfyUI's two JSON shapes into one Graph API so the resolver
doesn't need to know which shape it's looking at.

  "workflow" format (UI graph): {"nodes": [...], "links": [[id, orig_node,
      orig_slot, tgt_node, tgt_slot, type], ...], ...}
      - node.inputs is a list of {name, type, link}
      - node.widgets_values holds literal values for unlinked/widget inputs

  "prompt" format (execution graph): {node_id: {"class_type": ..., "inputs":
      {name: value_or_[src_node_id, src_slot]}, "_meta": {"title": ...}}}
      - values are inlined directly; no separate links array, no widgets

Both are exposed through the same Node/Graph interface:
    graph.get_input(node, name) -> ("node", Node) | ("literal", value) | ("none", None)
    graph.get_widget(node, index) -> value | None   (workflow format only)
"""

from __future__ import annotations

from typing import Any, Optional


class Node:
    __slots__ = ("id", "class_type", "title", "_raw", "_graph")

    def __init__(self, node_id: str, class_type: str, title: Optional[str], raw: dict, graph: "Graph"):
        self.id = node_id
        self.class_type = class_type
        self.title = title
        self._raw = raw
        self._graph = graph

    @property
    def widgets_values(self):
        return self._raw.get("widgets_values")

    def __repr__(self):
        t = self.title or self.class_type
        return f"<Node {self.id} {self.class_type!r} title={t!r}>"


# Fixed output-slot order for specific multi-output "pipe unwrapper" node
# types, used by Graph.output_name_at_slot() as a fallback when only
# "prompt" format is available (which carries no outputs list at all).
# Verified against multiple independently saved real workflows.
_KNOWN_UNWRAPPER_OUTPUT_ORDER: dict[str, tuple[str, ...]] = {
    "easy pipeOut": ("pipe", "model", "pos", "neg", "latent", "vae", "clip", "image", "seed"),
    "CR Module Input": ("pipe", "model", "pos", "neg", "latent", "vae", "clip", "controlnet", "image", "seed", "show_help"),
}


class Graph:
    def __init__(self):
        self.nodes: dict[str, Node] = {}
        self.format: str = ""  # "workflow" | "prompt"
        self._workflow_linkmap: dict[int, list] = {}

    # ---- construction -----------------------------------------------

    @classmethod
    def from_workflow(cls, workflow: dict) -> "Graph":
        g = cls()
        g.format = "workflow"
        g._workflow_linkmap = {l[0]: l for l in workflow.get("links", [])}
        for n in workflow.get("nodes", []):
            # Defensive against malformed/hand-edited workflow JSON: a node
            # entry missing "id" can't be referenced by any link anyway, so
            # it's safely skipped rather than crashing the whole graph
            # (regression fixture 18_node_missing_type_key.png). A node
            # missing "type" is kept (it may still be a legitimate link
            # target/source for other nodes) but degrades to class_type=""
            # -- matching from_prompt()'s existing default below -- which
            # simply won't match any known node type and falls through to
            # the same "[未対応ノード]"-style handling as a real unknown
            # class_type, instead of raising.
            if "id" not in n:
                continue
            node_id = str(n["id"])
            g.nodes[node_id] = Node(node_id, n.get("type", ""), n.get("title"), n, g)
        return g

    @classmethod
    def from_prompt(cls, prompt: dict) -> "Graph":
        g = cls()
        g.format = "prompt"
        for node_id, n in prompt.items():
            title = (n.get("_meta") or {}).get("title")
            g.nodes[str(node_id)] = Node(str(node_id), n.get("class_type", ""), title, n, g)
        return g

    # ---- lookup --------------------------------------------------------

    def get_input(self, node: Node, name: str):
        """Returns ('node', Node) | ('literal', value) | ('none', None)."""
        kind, val, _slot = self.get_input_with_slot(node, name)
        return kind, val

    def get_input_with_slot(self, node: Node, name: str):
        """Same as get_input(), but also returns which output slot index of
        the source node the link came from (None for literal/none).

        Every node type this resolver handled until "CR Module Input" had
        exactly one relevant output, so which slot fed a given link never
        mattered -- get_input() just discarded it. A node with several
        differently-named outputs (e.g. a pipe-bundle unwrapper exposing
        model/pos/neg/vae/... from one input) needs to know which specific
        output a downstream link used, since that's the only way to know
        which field the caller actually asked for.
        """
        if self.format == "workflow":
            for inp in node._raw.get("inputs", []):
                if inp.get("name") == name:
                    link_id = inp.get("link")
                    if link_id is None:
                        return ("none", None, None)
                    link = self._workflow_linkmap.get(link_id)
                    if not link:
                        return ("none", None, None)
                    src = self.nodes.get(str(link[1]))
                    if src is None:
                        return ("none", None, None)
                    slot = link[2] if len(link) > 2 else None
                    return ("node", src, slot)
            return ("none", None, None)

        # prompt format
        val = node._raw.get("inputs", {}).get(name)
        if val is None:
            return ("none", None, None)
        if isinstance(val, list) and len(val) == 2 and isinstance(val[0], (str, int)):
            src = self.nodes.get(str(val[0]))
            if src is not None:
                return ("node", src, val[1])
            return ("none", None, None)
        return ("literal", val, None)

    def output_name_at_slot(self, node: Node, slot: Optional[int]) -> Optional[str]:
        """Looks up a node's own declared output name at a given slot index.

        "workflow" format carries a per-node outputs list with real names;
        "prompt" format carries none at all (it's just a bare execution
        graph), so for a prompt-only graph this falls back to a hardcoded
        table for specific multi-output unwrapper node types this resolver
        needs slot-awareness for. Verified against several independently
        saved real workflows (same order every time) for the types listed;
        a node whose output order actually changed between package
        versions would silently misresolve here in prompt-only mode --
        accepted as a reasonable risk, since the alternative is simply
        never resolving prompt-format graphs through these nodes at all.
        """
        if slot is None:
            return None
        if self.format == "workflow":
            outputs = node._raw.get("outputs", [])
            if 0 <= slot < len(outputs):
                name = outputs[slot].get("name")
                if name:
                    return name
        order = _KNOWN_UNWRAPPER_OUTPUT_ORDER.get(node.class_type)
        if order and 0 <= slot < len(order):
            return order[slot]
        return None

    def get_widget(self, node: Node, index: int):
        """Only meaningful for workflow format; prompt format has no widgets."""
        if self.format != "workflow":
            return None
        wv = node.widgets_values
        if isinstance(wv, list) and 0 <= index < len(wv):
            return wv[index]
        return None

    def iter_connected_inputs(self, node: Node):
        """Yields (input_name, src_Node) for every input on this node that
        is wired to another node's output -- format-agnostic.

        Several resolvers (easy setNode/getNode, Any Switch (rgthree)) need
        to walk "whichever input happens to be connected" rather than one
        fixed input name, and used to do this by reaching directly into
        node._raw["inputs"] + graph._workflow_linkmap. That only works for
        the "workflow" shape (inputs = list of {name, link} dicts); in the
        "prompt" shape inputs is a {name: [src_id, slot]} dict, so iterating
        it yields bare name strings and any .get() call on them crashes.
        This method is the one place that knows the difference, so callers
        can go through graph.get_input()'s same abstraction instead of
        touching _raw directly.
        """
        if self.format == "workflow":
            for inp in node._raw.get("inputs", []):
                link_id = inp.get("link")
                if link_id is None:
                    continue
                link = self._workflow_linkmap.get(link_id)
                if not link:
                    continue
                src = self.nodes.get(str(link[1]))
                if src is not None:
                    yield inp.get("name", ""), src
            return

        # prompt format
        for name, val in node._raw.get("inputs", {}).items():
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], (str, int)):
                src = self.nodes.get(str(val[0]))
                if src is not None:
                    yield name, src

    def all_nodes_by_class(self, class_type: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.class_type == class_type]

    def is_active(self, node: Node) -> bool:
        """mode 0 = normal, 2 = muted, 4 = bypassed (workflow format only)."""
        if self.format != "workflow":
            return True
        return node._raw.get("mode", 0) == 0
