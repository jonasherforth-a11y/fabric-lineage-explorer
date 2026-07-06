"""
Diagram Renderer — Generates Mermaid diagrams and Pyvis interactive graphs.

Produces:
- ERD (Entity-Relationship Diagram) via Mermaid
- Full lineage flow diagram via Mermaid (Source → Table → Measure → Visual)
- Interactive network graph via Pyvis (for Streamlit embedding)
- Impact analysis highlight paths
"""

from lineage_engine import LineageEngine, GraphNode, GraphEdge


def generate_erd_mermaid(engine: LineageEngine, show_columns: bool = True, max_columns: int = 8, table_filter: set | None = None) -> str:
    """
    Generate a Mermaid ERD showing tables and their relationships.
    
    Args:
        engine: Built LineageEngine instance
        show_columns: Whether to show columns in table boxes
        max_columns: Max columns to display per table (avoid clutter)
        table_filter: If provided, only include tables whose name is in this set
    """
    lines = ["erDiagram"]

    tables = engine.get_all_nodes_by_type("table")
    if table_filter:
        tables = [t for t in tables if t.name in table_filter]

    table_names = {t.name for t in tables}

    relationships = [e for e in engine.edges if e.type == "has_relationship"
                     and e.from_id.startswith("table:") and e.to_id.startswith("table:")]

    # Deduplicate relationships (engine may add both directions)
    seen_rels = set()
    unique_rels = []
    for rel in relationships:
        from_t = rel.from_id.replace("table:", "")
        to_t = rel.to_id.replace("table:", "")
        if from_t not in table_names or to_t not in table_names:
            continue
        key = tuple(sorted([rel.from_id, rel.to_id]))
        if key not in seen_rels:
            seen_rels.add(key)
            unique_rels.append(rel)

    # Emit relationships
    for rel in unique_rels:
        from_name = _mermaid_safe(rel.from_id.replace("table:", ""))
        to_name = _mermaid_safe(rel.to_id.replace("table:", ""))
        lines.append(f"    {from_name} ||--o{{ {to_name} : \"\"")

    # Emit table definitions with columns
    if show_columns:
        for table in tables:
            table_name = _mermaid_safe(table.name)
            columns = [n for n in engine.nodes.values()
                       if n.type == "column" and n.table == table.name]
            measures = [n for n in engine.nodes.values()
                        if n.type == "measure" and n.table == table.name]

            lines.append(f"    {table_name} {{")

            # Show relationship columns first
            rel_cols = set()
            for edge in engine.edges:
                if edge.type == "has_relationship" and edge.from_id.startswith("column:"):
                    parts = edge.from_id.replace("column:", "").split(".", 1)
                    if len(parts) == 2 and parts[0] == table.name:
                        rel_cols.add(parts[1])
                if edge.type == "has_relationship" and edge.to_id.startswith("column:"):
                    parts = edge.to_id.replace("column:", "").split(".", 1)
                    if len(parts) == 2 and parts[0] == table.name:
                        rel_cols.add(parts[1])

            shown = 0
            for col in columns:
                if shown >= max_columns:
                    lines.append(f"        string _more_ \"{len(columns) - shown} more columns\"")
                    break
                dtype = _mermaid_dtype(col.detail.get("data_type", "string"))
                marker = " PK" if col.name in rel_cols else ""
                lines.append(f"        {dtype} {_mermaid_safe(col.name)}{marker}")
                shown += 1

            # Show measure count
            if measures:
                lines.append(f"        int _measures_ \"{len(measures)} measures\"")

            lines.append("    }")

    return "\n".join(lines)


def generate_lineage_mermaid(engine: LineageEngine, focus_node: str = None,
                              direction: str = "full",
                              visible_types: set = None, max_nodes: int = 200) -> str:
    """
    Generate a Mermaid flowchart showing the lineage flow.
    
    Direction: 'full' (all), 'upstream' (sources→node), 'downstream' (node→dependents)
    """
    if visible_types is None:
        visible_types = {"dataSource", "table", "column", "measure", "visual"}

    lines = ["flowchart LR"]

    if focus_node and focus_node in engine.nodes:
        if direction == "upstream":
            nodes_to_show = engine.get_upstream(focus_node)
            nodes_to_show.append(engine.nodes[focus_node])
        elif direction == "downstream":
            nodes_to_show = engine.get_downstream(focus_node)
            nodes_to_show.append(engine.nodes[focus_node])
        else:
            upstream = engine.get_upstream(focus_node)
            downstream = engine.get_downstream(focus_node)
            nodes_to_show = upstream + [engine.nodes[focus_node]] + downstream
        node_ids = {n.id for n in nodes_to_show if n.type in visible_types}
        node_ids.add(focus_node)  # Always include focus
    else:
        node_ids = {nid for nid, n in engine.nodes.items() if n.type in visible_types}

    # Cap node count
    if len(node_ids) > max_nodes:
        node_ids = set(list(node_ids)[:max_nodes])

    # Group by type for subgraphs (filtered by visible_types)
    sources = [n for n in engine.nodes.values() if n.type == "dataSource" and n.id in node_ids] if "dataSource" in visible_types else []
    tables = [n for n in engine.nodes.values() if n.type == "table" and n.id in node_ids] if "table" in visible_types else []
    columns = [n for n in engine.nodes.values() if n.type == "column" and n.id in node_ids] if "column" in visible_types else []
    measures = [n for n in engine.nodes.values() if n.type == "measure" and n.id in node_ids] if "measure" in visible_types else []
    visuals = [n for n in engine.nodes.values() if n.type == "visual" and n.id in node_ids] if "visual" in visible_types else []

    # Subgraph: Data Sources
    if sources:
        lines.append("    subgraph Sources")
        lines.append("        direction TB")
        for s in sources[:15]:
            safe_id = _mermaid_id(s.id)
            lines.append(f"        {safe_id}[(\"{_esc(s.name)}\")]")
        lines.append("    end")

    # Subgraph: Tables
    if tables:
        lines.append("    subgraph Tables")
        lines.append("        direction TB")
        for t in tables[:20]:
            safe_id = _mermaid_id(t.id)
            lines.append(f"        {safe_id}[\"{_esc(t.name)}\"]")
        lines.append("    end")

    # Subgraph: Columns
    if columns:
        lines.append("    subgraph Columns")
        lines.append("        direction TB")
        for c in columns[:30]:
            safe_id = _mermaid_id(c.id)
            lines.append(f"        {safe_id}[\"{_esc(c.table)}.{_esc(c.name)}\"]")
        lines.append("    end")

    # Subgraph: Measures
    if measures:
        lines.append("    subgraph Measures")
        lines.append("        direction TB")
        for m in measures[:30]:
            safe_id = _mermaid_id(m.id)
            lines.append(f"        {safe_id}([\"{_esc(m.name)}\"])")
        lines.append("    end")

    # Subgraph: Visuals
    if visuals:
        lines.append("    subgraph Visuals")
        lines.append("        direction TB")
        for v in visuals[:20]:
            safe_id = _mermaid_id(v.id)
            lines.append(f"        {safe_id}{{\"{_esc(v.name)}\"}}")
        lines.append("    end")

    # Edges (only between shown nodes)
    shown_edges = set()
    for edge in engine.edges:
        if edge.from_id in node_ids and edge.to_id in node_ids:
            from_safe = _mermaid_id(edge.from_id)
            to_safe = _mermaid_id(edge.to_id)
            edge_key = f"{from_safe}-->{to_safe}"
            if edge_key not in shown_edges:
                shown_edges.add(edge_key)
                lines.append(f"    {from_safe} --> {to_safe}")

    return "\n".join(lines)


def generate_measure_deps_mermaid(engine: LineageEngine, measure_name: str) -> str:
    """Generate a focused DAX dependency diagram for a single measure."""
    table = engine.measure_lookup.get(measure_name, "")
    if not table:
        return "flowchart LR\n    NoData[\"Measure not found\"]"

    root_id = f"measure:{table}.{measure_name}"
    upstream = engine.get_upstream(root_id, max_depth=5)

    lines = ["flowchart LR"]
    safe_root = _mermaid_id(root_id)
    lines.append(f"    {safe_root}([\"{_esc(measure_name)}\"]):::highlight")

    node_ids = {root_id}
    for n in upstream:
        node_ids.add(n.id)
        safe_id = _mermaid_id(n.id)
        if n.type == "measure":
            lines.append(f"    {safe_id}([\"{_esc(n.name)}\"])")
        elif n.type == "column":
            lines.append(f"    {safe_id}[\"{_esc(n.table)}.{_esc(n.name)}\"]")
        elif n.type == "table":
            lines.append(f"    {safe_id}[\"{_esc(n.name)}\"]")
        elif n.type == "dataSource":
            lines.append(f"    {safe_id}[(\"{_esc(n.name)}\")]")

    for edge in engine.edges:
        if edge.from_id in node_ids and edge.to_id in node_ids:
            lines.append(f"    {_mermaid_id(edge.from_id)} --> {_mermaid_id(edge.to_id)}")

    lines.append("    classDef highlight fill:#ff9800,stroke:#e65100,color:#fff")

    return "\n".join(lines)


def generate_impact_mermaid(engine: LineageEngine, node_id: str) -> str:
    """Generate an impact analysis diagram: what breaks if this node changes."""
    if node_id not in engine.nodes:
        return "flowchart LR\n    NoData[\"Node not found\"]"

    target = engine.nodes[node_id]
    downstream = engine.get_downstream(node_id, max_depth=5)

    lines = ["flowchart LR"]
    safe_root = _mermaid_id(node_id)
    lines.append(f"    {safe_root}[\"{_esc(target.name)}\"]:::impact")

    node_ids = {node_id}
    for n in downstream:
        node_ids.add(n.id)
        safe_id = _mermaid_id(n.id)
        if n.type == "visual":
            lines.append(f"    {safe_id}{{\"{_esc(n.name)}\"}}")
        elif n.type == "measure":
            lines.append(f"    {safe_id}([\"{_esc(n.name)}\"])")
        else:
            lines.append(f"    {safe_id}[\"{_esc(n.name)}\"]")

    for edge in engine.edges:
        if edge.from_id in node_ids and edge.to_id in node_ids:
            lines.append(f"    {_mermaid_id(edge.from_id)} --> {_mermaid_id(edge.to_id)}")

    lines.append("    classDef impact fill:#c62828,stroke:#b71c1c,color:#fff")

    return "\n".join(lines)


def generate_pyvis_html(engine: LineageEngine, focus_node: str = None,
                         height: str = "600px", width: str = "100%",
                         visible_types: set = None, max_nodes: int = 200) -> str:
    """
    Generate a standalone HTML page with an interactive Pyvis-style network graph.
    Uses vis.js directly (no Python pyvis dependency needed).
    """
    if visible_types is None:
        visible_types = {"dataSource", "table", "column", "measure", "visual"}

    if focus_node and focus_node in engine.nodes:
        upstream = engine.get_upstream(focus_node)
        downstream = engine.get_downstream(focus_node)
        relevant_nodes = upstream + [engine.nodes[focus_node]] + downstream
        node_ids = {n.id for n in relevant_nodes if n.type in visible_types}
        # Always include the focus node itself
        node_ids.add(focus_node)
    else:
        node_ids = {nid for nid, n in engine.nodes.items() if n.type in visible_types}

    # Cap node count to prevent vis.js from freezing
    if len(node_ids) > max_nodes:
        node_ids = set(list(node_ids)[:max_nodes])

    # Build vis.js nodes and edges
    vis_nodes = []
    vis_edges = []

    color_map = {
        "dataSource": {"background": "#4caf50", "border": "#2e7d32"},
        "table": {"background": "#1565c0", "border": "#0d47a1"},
        "column": {"background": "#42a5f5", "border": "#1565c0"},
        "measure": {"background": "#ff9800", "border": "#e65100"},
        "visual": {"background": "#9c27b0", "border": "#6a1b9a"},
    }

    shape_map = {
        "dataSource": "database",
        "table": "box",
        "column": "ellipse",
        "measure": "diamond",
        "visual": "star",
    }

    for node_id in node_ids:
        node = engine.nodes.get(node_id)
        if not node:
            continue
        colors = color_map.get(node.type, {"background": "#607d8b", "border": "#455a64"})
        shape = shape_map.get(node.type, "dot")
        label = node.name if len(node.name) <= 25 else node.name[:22] + "..."
        is_focus = node_id == focus_node

        vis_nodes.append({
            "id": node_id,
            "label": label,
            "title": f"{node.type}: {node.name}" + (f"\nTable: {node.table}" if node.table else ""),
            "color": colors,
            "shape": shape,
            "size": 30 if is_focus else 20,
            "borderWidth": 3 if is_focus else 1,
            "group": node.type,
        })

    seen = set()
    for edge in engine.edges:
        if edge.from_id in node_ids and edge.to_id in node_ids:
            key = (edge.from_id, edge.to_id)
            if key not in seen:
                seen.add(key)
                vis_edges.append({
                    "from": edge.from_id,
                    "to": edge.to_id,
                    "arrows": "to",
                    "color": {"color": "#90a4ae", "highlight": "#c89632"},
                })

    import json
    nodes_json = json.dumps(vis_nodes)
    edges_json = json.dumps(vis_edges)

    html = f"""<!DOCTYPE html>
<html><head>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
    #network {{ width: {width}; height: {height}; border: 1px solid #ddd; border-radius: 8px; }}
    .legend {{ display: flex; gap: 16px; padding: 8px; font-family: sans-serif; font-size: 12px; }}
    .legend-item {{ display: flex; align-items: center; gap: 4px; }}
    .legend-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
</style>
</head><body>
<div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#4caf50"></div>Data Source</div>
    <div class="legend-item"><div class="legend-dot" style="background:#1565c0"></div>Table</div>
    <div class="legend-item"><div class="legend-dot" style="background:#42a5f5"></div>Column</div>
    <div class="legend-item"><div class="legend-dot" style="background:#ff9800"></div>Measure</div>
    <div class="legend-item"><div class="legend-dot" style="background:#9c27b0"></div>Visual</div>
</div>
<div id="network"></div>
<script>
var nodes = new vis.DataSet({nodes_json});
var edges = new vis.DataSet({edges_json});
var container = document.getElementById('network');
var data = {{ nodes: nodes, edges: edges }};
var options = {{
    physics: {{ solver: 'forceAtlas2Based', forceAtlas2Based: {{ gravitationalConstant: -50, springLength: 120 }} }},
    interaction: {{ hover: true, tooltipDelay: 100 }},
    layout: {{ improvedLayout: true }},
    groups: {{
        dataSource: {{ color: {{background:'#4caf50',border:'#2e7d32'}} }},
        table: {{ color: {{background:'#1565c0',border:'#0d47a1'}} }},
        column: {{ color: {{background:'#42a5f5',border:'#1565c0'}} }},
        measure: {{ color: {{background:'#ff9800',border:'#e65100'}} }},
        visual: {{ color: {{background:'#9c27b0',border:'#6a1b9a'}} }}
    }}
}};
var network = new vis.Network(container, data, options);
</script>
</body></html>"""
    return html


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mermaid_safe(name: str) -> str:
    """Make a name safe for Mermaid identifiers."""
    # Replace common Danish/Nordic chars with ASCII equivalents
    _nordic = {"æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "å": "aa", "Å": "AA",
               "é": "e", "É": "E", "ü": "u", "Ü": "U", "ö": "o", "Ö": "O", "ä": "a", "Ä": "A"}
    safe = name
    for src, dst in _nordic.items():
        safe = safe.replace(src, dst)
    safe = safe.replace(" ", "_").replace("'", "").replace('"', "").replace("-", "_").replace(".", "_")
    safe = "".join(c for c in safe if c.isascii() and (c.isalnum() or c == "_"))
    if safe and safe[0].isdigit():
        safe = "T" + safe
    return safe or "unnamed"


def _mermaid_id(node_id: str) -> str:
    """Convert a node ID to a safe Mermaid ID."""
    return node_id.replace(":", "_").replace(".", "_").replace(" ", "_").replace("/", "_").replace("|", "_").replace("'", "").replace('"', "")


def _mermaid_dtype(dtype: str) -> str:
    """Map data types to Mermaid-friendly types."""
    mapping = {
        "int64": "int",
        "double": "float",
        "string": "string",
        "boolean": "bool",
        "dateTime": "datetime",
        "decimal": "decimal",
    }
    return mapping.get(dtype, "string")


def _esc(text: str) -> str:
    """Escape text for Mermaid labels."""
    return text.replace('"', "'").replace("\n", " ")[:40]
