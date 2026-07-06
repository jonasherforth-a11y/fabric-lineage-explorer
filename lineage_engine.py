"""
Lineage Engine — Builds a complete dependency graph for semantic models.

Nodes: data sources, tables, columns, measures, visuals
Edges: connects_to, belongs_to, references, depends_on, uses_field

Supports:
- Full lineage traversal (upstream/downstream)
- Impact analysis (select any node → see all dependents)
- Visual-to-source trace
- Broken reference detection
- Calculation group & field parameter awareness
"""

from dataclasses import dataclass, field
from typing import Optional
from pbip_insights import (
    extract_dax_references,
    build_measure_dependency_graph,
    extract_m_data_sources,
    DataSourceInfo,
    ReportPage,
    ReportVisual,
)


@dataclass
class GraphNode:
    """A node in the lineage graph."""
    id: str
    type: str  # dataSource, table, column, measure, visual, expression, calcItem, fpItem
    name: str
    table: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class GraphEdge:
    """An edge in the lineage graph."""
    from_id: str
    to_id: str
    type: str  # connects_to_source, belongs_to_table, references_column, depends_on_measure, uses_field, has_relationship


class LineageEngine:
    """
    Builds and queries a complete dependency graph from a parsed semantic model.
    
    Usage:
        engine = LineageEngine()
        engine.build_from_local(tables, relationships, pages)
        # or
        engine.build_from_api(tables_data, relationships_data, measures_data)
        
        # Query
        upstream = engine.get_upstream("measure:Sales[Total Revenue]")
        downstream = engine.get_downstream("table:DimCustomer")
        trace = engine.trace_visual_to_sources("visual:Page1|Chart1")
    """

    def __init__(self):
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self.measure_lookup: dict[str, str] = {}  # measure_name → table_name
        self.dep_graph: dict = {}  # from build_measure_dependency_graph
        self._edge_index_from: dict[str, list[GraphEdge]] = {}
        self._edge_index_to: dict[str, list[GraphEdge]] = {}

    def _add_node(self, node: GraphNode):
        self.nodes[node.id] = node

    def _add_edge(self, edge: GraphEdge):
        self.edges.append(edge)
        self._edge_index_from.setdefault(edge.from_id, []).append(edge)
        self._edge_index_to.setdefault(edge.to_id, []).append(edge)

    # ─── Build from local PBIP parse ─────────────────────────────────────────

    def build_from_local(self, tables: list, relationships: list, pages: list[ReportPage] = None):
        """
        Build graph from locally parsed TMDL data.
        
        Args:
            tables: list of TmdlTable dataclass instances
            relationships: list of TmdlRelationship dataclass instances
            pages: list of ReportPage (optional, for visual lineage)
        """
        self._clear()

        # Build measure lookup and DAX dependency graph
        self.dep_graph = build_measure_dependency_graph(tables)
        for table in tables:
            for measure in table.measures:
                self.measure_lookup[measure.name] = table.name

        # 1. Data sources from M expressions
        for table in tables:
            for partition in table.partitions:
                if partition.source_expression:
                    sources = extract_m_data_sources(
                        partition.source_expression, table.name, partition.name
                    )
                    for src in sources:
                        src_id = f"source:{src.source_type}://{src.server or src.url or src.path or src.database}"
                        if src_id not in self.nodes:
                            self._add_node(GraphNode(
                                id=src_id,
                                type="dataSource",
                                name=self._format_source_name(src),
                                detail={
                                    "source_type": src.source_type,
                                    "server": src.server,
                                    "database": src.database,
                                    "url": src.url,
                                    "path": src.path,
                                    "gateway": src.gateway_required,
                                }
                            ))
                        # Table → source edge
                        table_id = f"table:{table.name}"
                        self._add_edge(GraphEdge(table_id, src_id, "connects_to_source"))

        # 2. Tables, columns, measures
        for table in tables:
            table_id = f"table:{table.name}"
            self._add_node(GraphNode(
                id=table_id,
                type="table",
                name=table.name,
                detail={
                    "is_hidden": table.is_hidden,
                    "column_count": len(table.columns),
                    "measure_count": len(table.measures),
                    "partition_count": len(table.partitions),
                }
            ))

            # Columns
            for col in table.columns:
                col_id = f"column:{table.name}.{col.name}"
                self._add_node(GraphNode(
                    id=col_id,
                    type="column",
                    name=col.name,
                    table=table.name,
                    detail={
                        "data_type": col.data_type,
                        "is_hidden": col.is_hidden,
                        "source_column": col.source_column,
                    }
                ))
                self._add_edge(GraphEdge(col_id, table_id, "belongs_to_table"))

            # Measures
            for measure in table.measures:
                measure_id = f"measure:{table.name}.{measure.name}"
                refs = extract_dax_references(measure.expression, table.name)

                self._add_node(GraphNode(
                    id=measure_id,
                    type="measure",
                    name=measure.name,
                    table=table.name,
                    detail={
                        "expression": measure.expression,
                    }
                ))

                # Measure → column refs
                for tbl, col in refs.column_refs:
                    ref_col_id = f"column:{tbl}.{col}"
                    self._add_edge(GraphEdge(measure_id, ref_col_id, "references_column"))

                # Measure → measure refs
                for mref in refs.measure_refs:
                    ref_table = self.measure_lookup.get(mref, "")
                    if ref_table:
                        ref_measure_id = f"measure:{ref_table}.{mref}"
                        self._add_edge(GraphEdge(measure_id, ref_measure_id, "depends_on_measure"))

                # Measure → table refs (from DAX functions)
                for tref in refs.table_refs:
                    ref_table_id = f"table:{tref}"
                    self._add_edge(GraphEdge(measure_id, ref_table_id, "references_table"))

        # 3. Relationships
        for rel in relationships:
            from_id = f"table:{rel.from_table}"
            to_id = f"table:{rel.to_table}"
            self._add_edge(GraphEdge(from_id, to_id, "has_relationship"))
            # Also add column-level relationship edges
            from_col_id = f"column:{rel.from_table}.{rel.from_column}"
            to_col_id = f"column:{rel.to_table}.{rel.to_column}"
            self._add_edge(GraphEdge(from_col_id, to_col_id, "has_relationship"))

        # 4. Visuals
        if pages:
            for page in pages:
                for visual in page.visuals:
                    visual_id = f"visual:{page.display_name or page.name}|{visual.name}"
                    self._add_node(GraphNode(
                        id=visual_id,
                        type="visual",
                        name=visual.name,
                        detail={
                            "visual_type": visual.visual_type,
                            "page": page.display_name or page.name,
                        }
                    ))
                    for field_type, tbl, fld in visual.fields:
                        if field_type == "Measure":
                            target_id = f"measure:{tbl}.{fld}"
                        elif field_type == "Column":
                            target_id = f"column:{tbl}.{fld}"
                        else:
                            target_id = f"table:{tbl}"
                        self._add_edge(GraphEdge(visual_id, target_id, "uses_field"))

    # ─── Build from API data ─────────────────────────────────────────────────

    def build_from_api(self, tables_data: list[dict], relationships_data: list[dict],
                       measures_data: list[dict] = None, partitions_data: dict = None):
        """
        Build graph from Fabric API response data.
        
        Args:
            tables_data: [{name, columns: [{name, dataType, isHidden}], ...}]
            relationships_data: [{fromTable, fromColumn, toTable, toColumn, isActive}]
            measures_data: [{name, table, expression}] (optional)
            partitions_data: {table_name: [{name, source_expression}]} (optional)
        """
        self._clear()

        # Build measure lookup
        if measures_data:
            for m in measures_data:
                self.measure_lookup[m["name"]] = m.get("table", "")

        # Tables and columns
        for tbl in tables_data:
            table_name = tbl["name"]
            table_id = f"table:{table_name}"
            self._add_node(GraphNode(
                id=table_id,
                type="table",
                name=table_name,
                detail={
                    "is_hidden": tbl.get("isHidden", False),
                    "column_count": len(tbl.get("columns", [])),
                }
            ))

            for col in tbl.get("columns", []):
                col_id = f"column:{table_name}.{col['name']}"
                self._add_node(GraphNode(
                    id=col_id,
                    type="column",
                    name=col["name"],
                    table=table_name,
                    detail={
                        "data_type": col.get("dataType", ""),
                        "is_hidden": col.get("isHidden", False),
                    }
                ))
                self._add_edge(GraphEdge(col_id, table_id, "belongs_to_table"))

        # Measures
        if measures_data:
            for m in measures_data:
                measure_id = f"measure:{m['table']}.{m['name']}"
                refs = extract_dax_references(m.get("expression", ""), m.get("table", ""))

                self._add_node(GraphNode(
                    id=measure_id,
                    type="measure",
                    name=m["name"],
                    table=m.get("table", ""),
                    detail={"expression": m.get("expression", "")}
                ))

                for tbl, col in refs.column_refs:
                    self._add_edge(GraphEdge(measure_id, f"column:{tbl}.{col}", "references_column"))
                for mref in refs.measure_refs:
                    ref_table = self.measure_lookup.get(mref, "")
                    if ref_table:
                        self._add_edge(GraphEdge(measure_id, f"measure:{ref_table}.{mref}", "depends_on_measure"))
                for tref in refs.table_refs:
                    self._add_edge(GraphEdge(measure_id, f"table:{tref}", "references_table"))

            # Build dep_graph for convenience
            self.dep_graph = {}
            for m in measures_data:
                refs = extract_dax_references(m.get("expression", ""), m.get("table", ""))
                self.dep_graph[m["name"]] = {
                    "table": m.get("table", ""),
                    "expression": m.get("expression", ""),
                    "depends_on_measures": refs.measure_refs,
                    "depends_on_columns": refs.column_refs,
                    "depends_on_tables": refs.table_refs,
                }

        # Relationships
        for rel in relationships_data:
            from_id = f"table:{rel['fromTable']}"
            to_id = f"table:{rel['toTable']}"
            self._add_edge(GraphEdge(from_id, to_id, "has_relationship"))
            self._add_edge(GraphEdge(
                f"column:{rel['fromTable']}.{rel['fromColumn']}",
                f"column:{rel['toTable']}.{rel['toColumn']}",
                "has_relationship"
            ))

        # Partitions / data sources
        if partitions_data:
            for table_name, parts in partitions_data.items():
                for p in parts:
                    if p.get("source_expression"):
                        sources = extract_m_data_sources(p["source_expression"], table_name, p.get("name", ""))
                        for src in sources:
                            src_id = f"source:{src.source_type}://{src.server or src.url or src.path or src.database}"
                            if src_id not in self.nodes:
                                self._add_node(GraphNode(
                                    id=src_id,
                                    type="dataSource",
                                    name=self._format_source_name(src),
                                    detail={
                                        "source_type": src.source_type,
                                        "server": src.server,
                                        "database": src.database,
                                        "url": src.url,
                                        "path": src.path,
                                    }
                                ))
                            self._add_edge(GraphEdge(f"table:{table_name}", src_id, "connects_to_source"))

    # ─── Graph Queries ────────────────────────────────────────────────────────

    def get_upstream(self, node_id: str, max_depth: int = 10) -> list[GraphNode]:
        """Get all upstream dependencies (what this node depends on)."""
        visited = set()
        result = []
        self._traverse_upstream(node_id, visited, result, 0, max_depth)
        return result

    def get_downstream(self, node_id: str, max_depth: int = 10) -> list[GraphNode]:
        """Get all downstream dependents (what depends on this node)."""
        visited = set()
        result = []
        self._traverse_downstream(node_id, visited, result, 0, max_depth)
        return result

    def trace_visual_to_sources(self, visual_id: str) -> dict:
        """Trace a visual all the way back to data sources."""
        upstream = self.get_upstream(visual_id)
        return {
            "visual": self.nodes.get(visual_id),
            "measures": [n for n in upstream if n.type == "measure"],
            "columns": [n for n in upstream if n.type == "column"],
            "tables": [n for n in upstream if n.type == "table"],
            "data_sources": [n for n in upstream if n.type == "dataSource"],
        }

    def get_impact(self, node_id: str) -> dict:
        """Impact analysis: what is affected if this node changes."""
        downstream = self.get_downstream(node_id)
        return {
            "node": self.nodes.get(node_id),
            "affected_measures": [n for n in downstream if n.type == "measure"],
            "affected_visuals": [n for n in downstream if n.type == "visual"],
            "affected_tables": [n for n in downstream if n.type == "table"],
            "total_impact": len(downstream),
        }

    def get_broken_references(self) -> list[dict]:
        """Find edges that point to non-existent nodes (stale references)."""
        broken = []
        for edge in self.edges:
            if edge.to_id not in self.nodes and edge.from_id in self.nodes:
                broken.append({
                    "from_node": self.nodes[edge.from_id].name,
                    "from_type": self.nodes[edge.from_id].type,
                    "missing_target": edge.to_id,
                    "edge_type": edge.type,
                })
        return broken

    def get_unused_columns(self) -> list[GraphNode]:
        """Find columns that are never referenced by any measure or visual."""
        used_columns = set()
        for edge in self.edges:
            if edge.type in ("references_column", "uses_field") and edge.to_id.startswith("column:"):
                used_columns.add(edge.to_id)
            # Also count relationship columns as "used"
            if edge.type == "has_relationship" and edge.from_id.startswith("column:"):
                used_columns.add(edge.from_id)
            if edge.type == "has_relationship" and edge.to_id.startswith("column:"):
                used_columns.add(edge.to_id)

        unused = []
        for node_id, node in self.nodes.items():
            if node.type == "column" and node_id not in used_columns:
                unused.append(node)
        return unused

    def get_table_summary(self, table_name: str) -> dict:
        """Get a comprehensive summary of a table."""
        table_id = f"table:{table_name}"
        if table_id not in self.nodes:
            return {}

        columns = [n for n in self.nodes.values() if n.type == "column" and n.table == table_name]
        measures = [n for n in self.nodes.values() if n.type == "measure" and n.table == table_name]

        # Find relationships
        rels = []
        for edge in self._edge_index_from.get(table_id, []):
            if edge.type == "has_relationship":
                rels.append({"direction": "from", "target": edge.to_id})
        for edge in self._edge_index_to.get(table_id, []):
            if edge.type == "has_relationship":
                rels.append({"direction": "to", "source": edge.from_id})

        # Find data sources
        sources = []
        for edge in self._edge_index_from.get(table_id, []):
            if edge.type == "connects_to_source":
                src = self.nodes.get(edge.to_id)
                if src:
                    sources.append(src)

        # Downstream visuals using this table's columns/measures
        downstream_visuals = set()
        for col in columns:
            for edge in self._edge_index_to.get(col.id, []):
                if edge.type == "uses_field" and edge.from_id.startswith("visual:"):
                    downstream_visuals.add(edge.from_id)
        for meas in measures:
            for edge in self._edge_index_to.get(meas.id, []):
                if edge.type == "uses_field" and edge.from_id.startswith("visual:"):
                    downstream_visuals.add(edge.from_id)

        return {
            "name": table_name,
            "columns": columns,
            "measures": measures,
            "relationships": rels,
            "data_sources": sources,
            "visual_consumers": len(downstream_visuals),
            "is_hidden": self.nodes[table_id].detail.get("is_hidden", False),
        }

    def get_stats(self) -> dict:
        """Get graph statistics."""
        type_counts = {}
        for node in self.nodes.values():
            type_counts[node.type] = type_counts.get(node.type, 0) + 1

        edge_type_counts = {}
        for edge in self.edges:
            edge_type_counts[edge.type] = edge_type_counts.get(edge.type, 0) + 1

        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "nodes_by_type": type_counts,
            "edges_by_type": edge_type_counts,
        }

    def get_all_nodes_by_type(self, node_type: str) -> list[GraphNode]:
        """Get all nodes of a specific type."""
        return [n for n in self.nodes.values() if n.type == node_type]

    # ─── Internal helpers ─────────────────────────────────────────────────────

    def _traverse_upstream(self, node_id: str, visited: set, result: list, depth: int, max_depth: int):
        """Traverse upstream (follow outgoing edges from this node)."""
        if depth >= max_depth or node_id in visited:
            return
        visited.add(node_id)

        for edge in self._edge_index_from.get(node_id, []):
            target = self.nodes.get(edge.to_id)
            if target and edge.to_id not in visited:
                result.append(target)
                self._traverse_upstream(edge.to_id, visited, result, depth + 1, max_depth)

    def _traverse_downstream(self, node_id: str, visited: set, result: list, depth: int, max_depth: int):
        """Traverse downstream (follow incoming edges to this node)."""
        if depth >= max_depth or node_id in visited:
            return
        visited.add(node_id)

        for edge in self._edge_index_to.get(node_id, []):
            source = self.nodes.get(edge.from_id)
            if source and edge.from_id not in visited:
                result.append(source)
                self._traverse_downstream(edge.from_id, visited, result, depth + 1, max_depth)

    def _format_source_name(self, src: DataSourceInfo) -> str:
        """Format a data source into a readable name."""
        if src.server and src.database:
            return f"{src.source_type}: {src.server}/{src.database}"
        elif src.server:
            return f"{src.source_type}: {src.server}"
        elif src.url:
            return f"{src.source_type}: {src.url}"
        elif src.path:
            return f"{src.source_type}: {src.path}"
        return src.source_type

    def _clear(self):
        """Reset the graph."""
        self.nodes.clear()
        self.edges.clear()
        self.measure_lookup.clear()
        self.dep_graph.clear()
        self._edge_index_from.clear()
        self._edge_index_to.clear()
