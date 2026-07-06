"""
Model Detail — Table/column explorer views for Streamlit.

Provides rich detail views:
- Table inventory with stats
- Column detail (data type, relationships, usage, hidden status)
- Measure catalog with DAX expressions and dependencies
- Relationship matrix
- Unused/orphan detection
"""

from lineage_engine import LineageEngine, GraphNode


def get_table_inventory(engine: LineageEngine) -> list[dict]:
    """Get a summary inventory of all tables."""
    tables = engine.get_all_nodes_by_type("table")
    inventory = []

    for table in tables:
        summary = engine.get_table_summary(table.name)
        inventory.append({
            "name": table.name,
            "columns": len(summary.get("columns", [])),
            "measures": len(summary.get("measures", [])),
            "relationships": len(summary.get("relationships", [])),
            "data_sources": len(summary.get("data_sources", [])),
            "visual_consumers": summary.get("visual_consumers", 0),
            "is_hidden": summary.get("is_hidden", False),
        })

    # Sort: tables with most relationships first
    inventory.sort(key=lambda x: (-x["relationships"], -x["columns"], x["name"]))
    return inventory


def get_column_detail(engine: LineageEngine, table_name: str) -> list[dict]:
    """Get detailed column information for a table."""
    columns = [n for n in engine.nodes.values()
               if n.type == "column" and n.table == table_name]

    details = []
    for col in columns:
        # Find what references this column
        referencing_measures = []
        referencing_visuals = []
        relationships = []

        for edge in engine._edge_index_to.get(col.id, []):
            if edge.type == "references_column":
                src = engine.nodes.get(edge.from_id)
                if src and src.type == "measure":
                    referencing_measures.append(src.name)
            elif edge.type == "uses_field":
                src = engine.nodes.get(edge.from_id)
                if src and src.type == "visual":
                    referencing_visuals.append(src.name)
            elif edge.type == "has_relationship":
                src = engine.nodes.get(edge.from_id)
                if src:
                    relationships.append(f"← {src.table}.{src.name}")

        for edge in engine._edge_index_from.get(col.id, []):
            if edge.type == "has_relationship" and edge.to_id.startswith("column:"):
                tgt = engine.nodes.get(edge.to_id)
                if tgt:
                    relationships.append(f"→ {tgt.table}.{tgt.name}")

        is_used = bool(referencing_measures or referencing_visuals or relationships)

        details.append({
            "name": col.name,
            "data_type": col.detail.get("data_type", ""),
            "is_hidden": col.detail.get("is_hidden", False),
            "source_column": col.detail.get("source_column", ""),
            "used_by_measures": referencing_measures,
            "used_by_visuals": referencing_visuals,
            "relationships": relationships,
            "is_used": is_used,
            "usage_count": len(referencing_measures) + len(referencing_visuals),
        })

    # Sort: relationship columns first, then by usage
    details.sort(key=lambda x: (-len(x["relationships"]), -x["usage_count"], x["name"]))
    return details


def get_measure_catalog(engine: LineageEngine, table_name: str = None) -> list[dict]:
    """Get measure catalog with DAX expressions and dependency info."""
    measures = [n for n in engine.nodes.values() if n.type == "measure"]
    if table_name:
        measures = [m for m in measures if m.table == table_name]

    catalog = []
    for measure in measures:
        deps = engine.dep_graph.get(measure.name, {})
        upstream = deps.get("depends_on_measures", [])
        
        # Impact: who depends on this measure
        downstream = []
        for name, info in engine.dep_graph.items():
            if measure.name in info.get("depends_on_measures", []):
                downstream.append(name)

        # Which visuals use this measure
        visual_users = []
        for edge in engine._edge_index_to.get(measure.id, []):
            if edge.type == "uses_field":
                src = engine.nodes.get(edge.from_id)
                if src and src.type == "visual":
                    visual_users.append(src.name)

        catalog.append({
            "name": measure.name,
            "table": measure.table,
            "expression": measure.detail.get("expression", ""),
            "depends_on": upstream,
            "depended_by": downstream,
            "visual_users": visual_users,
            "column_refs": deps.get("depends_on_columns", []),
            "table_refs": deps.get("depends_on_tables", []),
            "complexity": _measure_complexity(measure.detail.get("expression", "")),
        })

    catalog.sort(key=lambda x: (-len(x["depended_by"]), -x["complexity"], x["name"]))
    return catalog


def get_relationship_matrix(engine: LineageEngine) -> list[dict]:
    """Get all relationships as a list for display."""
    rels = []
    seen = set()

    for edge in engine.edges:
        if edge.type == "has_relationship" and edge.from_id.startswith("column:"):
            key = tuple(sorted([edge.from_id, edge.to_id]))
            if key in seen:
                continue
            seen.add(key)

            from_parts = edge.from_id.replace("column:", "").split(".", 1)
            to_parts = edge.to_id.replace("column:", "").split(".", 1)
            if len(from_parts) == 2 and len(to_parts) == 2:
                rels.append({
                    "from_table": from_parts[0],
                    "from_column": from_parts[1],
                    "to_table": to_parts[0],
                    "to_column": to_parts[1],
                })

    return rels


def get_data_source_inventory(engine: LineageEngine) -> list[dict]:
    """Get all data sources with their consuming tables."""
    sources = engine.get_all_nodes_by_type("dataSource")
    inventory = []

    for src in sources:
        # Find which tables connect to this source
        consuming_tables = []
        for edge in engine._edge_index_to.get(src.id, []):
            if edge.type == "connects_to_source":
                tbl = engine.nodes.get(edge.from_id)
                if tbl:
                    consuming_tables.append(tbl.name)

        inventory.append({
            "name": src.name,
            "type": src.detail.get("source_type", ""),
            "server": src.detail.get("server", ""),
            "database": src.detail.get("database", ""),
            "url": src.detail.get("url", ""),
            "tables": consuming_tables,
            "table_count": len(consuming_tables),
        })

    inventory.sort(key=lambda x: (-x["table_count"], x["name"]))
    return inventory


def _measure_complexity(expression: str) -> int:
    """Simple complexity score based on DAX expression characteristics."""
    if not expression:
        return 0
    score = 0
    score += expression.count("CALCULATE") * 3
    score += expression.count("FILTER") * 2
    score += expression.count("SUMX") * 2
    score += expression.count("AVERAGEX") * 2
    score += expression.count("VAR ") * 1
    score += expression.count("SWITCH") * 2
    score += expression.count("IF(") * 1
    score += len(expression) // 100  # Length factor
    return score
