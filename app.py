"""
Lineage Explorer — Interactive Streamlit UI

Provides:
- Dashboard overview (reports, models, sources, issues)
- Interactive lineage graph (report → model → data sources)
- Filterable tables with drill-down
- Issue tracker with severity
- Search across all artifacts
- Live scan or load from JSON
"""

import json
import sys
from pathlib import Path

import streamlit as st

# Add current dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from tmdl_parser import scan_solution_folder
from ci_lineage_check import build_lineage_map, generate_summary
from pbip_insights import (
    scan_semantic_model_enhanced,
    scan_report_enhanced,
    extract_dax_references,
    build_measure_dependency_graph,
    resolve_measure_chain,
    get_measure_impact,
    build_field_usage_map,
    parse_report_pages,
    DataSourceInfo,
)

# ─── Page Config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Lineage Explorer",
    page_icon="🔗",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 1.2rem;
        border-radius: 0.8rem;
        color: white;
        text-align: center;
    }
    .metric-card h2 { margin: 0; font-size: 2rem; }
    .metric-card p { margin: 0; opacity: 0.85; font-size: 0.9rem; }
    .issue-error { border-left: 4px solid #e74c3c; padding: 0.5rem 1rem; margin: 0.3rem 0; background: #fdf0f0; border-radius: 0 0.4rem 0.4rem 0; }
    .issue-warning { border-left: 4px solid #f39c12; padding: 0.5rem 1rem; margin: 0.3rem 0; background: #fef9e7; border-radius: 0 0.4rem 0.4rem 0; }
    .source-badge { display: inline-block; padding: 0.2rem 0.6rem; margin: 0.1rem; border-radius: 1rem; font-size: 0.75rem; font-weight: 500; }
    .source-sql { background: #dbeafe; color: #1e40af; }
    .source-file { background: #dcfce7; color: #166534; }
    .source-web { background: #fef3c7; color: #92400e; }
    .source-sp { background: #ede9fe; color: #5b21b6; }
    div[data-testid="stMetric"] { background: #f8fafc; padding: 1rem; border-radius: 0.5rem; border: 1px solid #e2e8f0; }
</style>
""", unsafe_allow_html=True)


# ─── State & Data Loading ───────────────────────────────────────────────────────
@st.cache_data
def load_from_scan(solution_path: str) -> dict:
    """Scan solution folder and build lineage."""
    path = Path(solution_path)
    result = scan_solution_folder(path)
    lineage = build_lineage_map(result["models"], result["reports"])
    # Add raw model/report objects for detail views
    lineage["_models_raw"] = result["models"]
    lineage["_reports_raw"] = result["reports"]
    lineage["_solution_path"] = solution_path
    return lineage


@st.cache_data
def load_from_json(json_path: str) -> dict:
    """Load pre-generated lineage JSON."""
    return json.loads(Path(json_path).read_text(encoding="utf-8"))


def load_from_api(workspace_name: str) -> dict:
    """Load lineage from live Fabric/Power BI API."""
    from lineage_tracker import FabricLineageTracker

    try:
        tracker = FabricLineageTracker()
    except Exception as e:
        st.error(f"Authentication failed: {e}")
        return None

    ws = tracker.find_workspace(workspace_name)
    if not ws:
        st.error(f"Workspace '{workspace_name}' not found. Check the name and your permissions.")
        return None

    workspace_id = ws["id"]
    st.info(f"Connected to workspace: **{ws['displayName']}** (`{workspace_id}`)")

    try:
        reports_raw = tracker.list_reports(workspace_id)
    except Exception as e:
        st.error(f"Failed to list reports: {e}")
        return None

    if not reports_raw:
        st.warning("No reports found in this workspace.")

    # Also list datasets (semantic models) directly — handles model-only workspaces
    try:
        datasets_raw = tracker.list_datasets(workspace_id)
    except Exception:
        datasets_raw = []

    models = []
    reports = []
    all_data_sources = set()
    issues = []
    bindings = []
    model_cache = {}

    for rpt in reports_raw:
        report_id = rpt["id"]
        report_name = rpt.get("displayName", rpt.get("name", "Unknown"))

        # Get report details (dataset binding)
        try:
            details = tracker.get_report_details(workspace_id, report_id)
            dataset_id = details.get("datasetId", "")
        except Exception as e:
            dataset_id = ""
            issues.append({"severity": "error", "message": f"Failed to get details for report '{report_name}': {e}"})

        # Get pages
        try:
            pages = tracker.get_report_pages(workspace_id, report_id)
            page_names = [p.get("displayName", p.get("name", "")) for p in pages]
        except Exception:
            page_names = []

        reports.append({
            "name": report_name,
            "report_id": report_id,
            "semantic_model": dataset_id,
            "bound_model": "",  # Will be resolved after model scan
            "pages": page_names,
            "workspace": workspace_name,
            "path": "",
        })

        # Get semantic model if not cached
        if dataset_id and dataset_id not in model_cache:
            model_info = {"name": "", "tables": [], "data_sources": [], "relationships": 0, "total_measures": 0, "path": ""}
            try:
                ds_info = tracker.get_dataset_info(workspace_id, dataset_id)
                model_info["name"] = ds_info.get("name", dataset_id)
            except Exception:
                model_info["name"] = dataset_id

            # Try DAX queries for rich metadata
            try:
                tables = tracker.get_model_tables_via_dax(workspace_id, dataset_id)
                columns = tracker.get_model_columns_via_dax(workspace_id, dataset_id)
                for t in tables:
                    t.columns = columns.get(t.name, [])
                model_info["tables"] = [
                    {"name": t.name, "columns": len(t.columns), "measures": 0, "is_hidden": t.is_hidden}
                    for t in tables
                ]
            except Exception:
                # Fallback: REST tables
                try:
                    rest_tables = tracker.get_dataset_tables(workspace_id, dataset_id)
                    model_info["tables"] = [
                        {"name": t.get("name", ""), "columns": 0, "measures": 0, "is_hidden": False}
                        for t in rest_tables
                    ]
                except Exception:
                    pass

            # Get measures
            try:
                measures = tracker.get_model_measures_via_dax(workspace_id, dataset_id)
                model_info["total_measures"] = len(measures)
            except Exception:
                pass

            # Get relationships
            try:
                rels = tracker.get_model_relationships_via_dax(workspace_id, dataset_id)
                model_info["relationships"] = len(rels)
            except Exception:
                pass

            # Get data sources from partitions
            try:
                partitions = tracker.get_partitions_via_dax(workspace_id, dataset_id)
                for table_name, parts in partitions.items():
                    for p in parts:
                        if p.source_expression:
                            from pbip_insights import extract_m_data_sources
                            sources = extract_m_data_sources(p.source_expression, table_name, p.name)
                            for s in sources:
                                src_label = f"{s.source_type}://{s.server or s.url or s.path}"
                                model_info["data_sources"].append(src_label)
                                all_data_sources.add(src_label)
            except Exception:
                pass

            model_info["data_sources"] = list(set(model_info["data_sources"]))
            model_cache[dataset_id] = model_info
            models.append(model_info)

        # Build binding
        cached = model_cache.get(dataset_id, {})
        bindings.append({
            "report": report_name,
            "model": cached.get("name", dataset_id),
            "sources": cached.get("data_sources", []),
        })

    # Check for issues
    for rpt in reports:
        if not rpt.get("semantic_model"):
            issues.append({"severity": "error", "message": f"Report '{rpt['name']}' has no dataset binding"})

    # Discover models directly (for model-only workspaces or models not bound to reports)
    for ds in datasets_raw:
        dataset_id = ds.get("id", "")
        if dataset_id and dataset_id not in model_cache:
            model_info = {"name": ds.get("name", dataset_id), "tables": [], "data_sources": [], "relationships": 0, "total_measures": 0, "path": ""}

            # Try DAX queries for rich metadata
            try:
                tables = tracker.get_model_tables_via_dax(workspace_id, dataset_id)
                columns = tracker.get_model_columns_via_dax(workspace_id, dataset_id)
                for t in tables:
                    t.columns = columns.get(t.name, [])
                model_info["tables"] = [
                    {"name": t.name, "columns": len(t.columns), "measures": 0, "is_hidden": t.is_hidden}
                    for t in tables
                ]
            except Exception:
                try:
                    rest_tables = tracker.get_dataset_tables(workspace_id, dataset_id)
                    model_info["tables"] = [
                        {"name": t.get("name", ""), "columns": 0, "measures": 0, "is_hidden": False}
                        for t in rest_tables
                    ]
                except Exception:
                    pass

            # Get measures
            try:
                measures = tracker.get_model_measures_via_dax(workspace_id, dataset_id)
                model_info["total_measures"] = len(measures)
            except Exception:
                pass

            # Get relationships
            try:
                rels = tracker.get_model_relationships_via_dax(workspace_id, dataset_id)
                model_info["relationships"] = len(rels)
            except Exception:
                pass

            # Get data sources from partitions
            try:
                partitions = tracker.get_partitions_via_dax(workspace_id, dataset_id)
                for table_name, parts in partitions.items():
                    for p in parts:
                        if p.source_expression:
                            from pbip_insights import extract_m_data_sources
                            sources = extract_m_data_sources(p.source_expression, table_name, p.name)
                            for s in sources:
                                src_label = f"{s.source_type}://{s.server or s.url or s.path}"
                                model_info["data_sources"].append(src_label)
                                all_data_sources.add(src_label)
            except Exception:
                pass

            model_info["data_sources"] = list(set(model_info["data_sources"]))
            model_cache[dataset_id] = model_info
            models.append(model_info)

    # Resolve bound_model names
    for rpt in reports:
        dataset_id = rpt.get("semantic_model", "")
        if dataset_id and dataset_id in model_cache:
            rpt["bound_model"] = model_cache[dataset_id].get("name", dataset_id)

    st.success(f"✅ Found {len(reports)} reports, {len(models)} models, {len(all_data_sources)} data sources")

    return {
        "models": models,
        "reports": reports,
        "data_sources": list(all_data_sources),
        "bindings": bindings,
        "issues": issues,
        "summary": {
            "total_reports": len(reports),
            "total_models": len(models),
            "total_data_sources": len(all_data_sources),
            "total_issues": len(issues),
        },
        "_models_raw": None,
        "_reports_raw": None,
    }


def classify_source(source: str) -> str:
    if source.startswith("SQL://"):
        return "sql"
    elif source.startswith("File://"):
        return "file"
    elif source.startswith("Web://"):
        return "web"
    elif source.startswith("SharePoint://"):
        return "sp"
    return "other"


def source_badge(source: str) -> str:
    cls = classify_source(source)
    return f'<span class="source-badge source-{cls}">{source}</span>'


def _get(obj, key, default=None):
    """Get attribute or dict key — handles both dataclass instances and dicts."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ─── Sidebar ────────────────────────────────────────────────────────────────────
# Auto-detect if running on Streamlit Cloud (no local filesystem access)
_DEMO_JSON = Path(__file__).parent / "data" / "demo_lineage.json"
_IS_CLOUD = not Path(r"c:\Users").exists()  # Simple: Windows paths don't exist on Linux cloud

with st.sidebar:
    st.image(str(Path(__file__).parent / "assets" / "logo.png"), width=120)
    st.title("Lineage Explorer")
    st.markdown("---")

    if _IS_CLOUD:
        load_mode = st.radio("Data Source", ["Athene Lineage Overview", "Upload JSON", "Fabric API"], horizontal=True)
    else:
        load_mode = st.radio("Data Source", ["Local PBIP", "Upload JSON", "Fabric API"], horizontal=True)

    scan_btn = False
    if load_mode == "Local PBIP":
        solution_path = st.text_input("Solution folder path", placeholder="e.g. C:/repos/my-project/solution")
        scan_btn = st.button("🔍 Scan Now", type="primary", use_container_width=True)
    elif load_mode == "Athene Lineage Overview":
        st.caption("Pre-loaded lineage data from the Athene Power BI solution with 66 reports, 31 models, 267 tables.")
        scan_btn = st.button("📊 Load Athene", type="primary", use_container_width=True)
    elif load_mode == "Upload JSON":
        uploaded_file = st.file_uploader("Upload lineage JSON", type=["json"], help="Upload a previously exported lineage JSON file")
        if uploaded_file is not None:
            scan_btn = True
    elif load_mode == "Fabric API":
        workspace_name = st.text_input("Workspace name", placeholder="e.g. My Workspace")
        st.caption("Uses Azure CLI auth (MSAL interactive login)")
        col_auth, col_scan = st.columns(2)
        with col_auth:
            if st.button("🔑 Authenticate"):
                try:
                    from lineage_tracker import get_fabric_token, get_pbi_token
                    get_fabric_token()
                    get_pbi_token()
                    st.session_state["api_authenticated"] = True
                    st.success("Authenticated!")
                except Exception as e:
                    st.error(f"Auth failed: {e}")
        with col_scan:
            scan_btn = st.button("☁️ Connect & Scan", type="primary")

    st.markdown("---")
    st.markdown("**Navigation**")
    page = st.radio(
        "View",
        ["📊 Dashboard", "🔗 Lineage Graph", "🗺️ ERD Diagram", "🌊 Lineage Flow", "🎯 Impact Analysis",
         "📐 Model Explorer", "🧬 Model Insights", "📑 Report Insights", "📋 Reports", "🧊 Models", "⚠️ Issues", "🔎 Search"],
        label_visibility="collapsed",
    )

# ─── Load Data ──────────────────────────────────────────────────────────────────
lineage = None

# Auto-load demo data on first visit if available
if "lineage" not in st.session_state and _DEMO_JSON.exists():
    lineage = load_from_json(str(_DEMO_JSON))
    st.session_state["lineage"] = lineage

if scan_btn:
    if load_mode == "Local PBIP":
        if not solution_path:
            st.warning("Please enter a solution folder path.")
        else:
            with st.spinner("Scanning PBIP artifacts..."):
                lineage = load_from_scan(solution_path)
    elif load_mode == "Athene Lineage Overview":
        with st.spinner("Loading Athene lineage..."):
            lineage = load_from_json(str(_DEMO_JSON))
    elif load_mode == "Upload JSON":
        with st.spinner("Loading uploaded JSON..."):
            lineage = json.loads(uploaded_file.read().decode("utf-8"))
    elif load_mode == "Fabric API":
        if not workspace_name:
            st.warning("Please enter a workspace name.")
        else:
            with st.spinner(f"Connecting to Fabric API — workspace '{workspace_name}'..."):
                lineage = load_from_api(workspace_name)
    if lineage:
        st.session_state["lineage"] = lineage
        # Clear cached engine so it rebuilds with new data
        if "lineage_engine" in st.session_state:
            del st.session_state["lineage_engine"]

if "lineage" in st.session_state:
    lineage = st.session_state["lineage"]

if lineage is None:
    st.info("👈 Configure a data source in the sidebar and click Scan/Load to begin.")
    st.stop()


# ─── Helper: Build engine from lineage data ─────────────────────────────────────
def _build_engine_from_lineage(lineage_data: dict):
    """Build a LineageEngine from current lineage state (local or API)."""
    from lineage_engine import LineageEngine
    engine = LineageEngine()

    models_raw = lineage_data.get("_models_raw")
    if models_raw and len(models_raw) > 0:
        first = models_raw[0]
        if hasattr(first, "tables") and not isinstance(first, dict):
            # Local PBIP mode: models_raw are SemanticModelDef dataclass instances
            all_tables = []
            all_relationships = []
            for model in models_raw:
                all_tables.extend(model.tables)
                all_relationships.extend(model.relationships)
            engine.build_from_local(all_tables, all_relationships, [])
        else:
            # JSON mode: models_raw are dicts — convert to simple namespace objects
            from types import SimpleNamespace
            all_tables = []
            all_relationships = []
            for model in models_raw:
                for t_dict in model.get("tables", []):
                    table = SimpleNamespace(
                        name=t_dict.get("name", ""),
                        is_hidden=t_dict.get("is_hidden", False),
                        columns=[SimpleNamespace(
                            name=c.get("name", ""),
                            data_type=c.get("data_type", ""),
                            source_column=c.get("source_column", ""),
                            is_hidden=c.get("is_hidden", False),
                        ) for c in t_dict.get("columns", [])],
                        measures=[SimpleNamespace(
                            name=m.get("name", ""),
                            expression=m.get("expression", ""),
                            table=m.get("table", ""),
                        ) for m in t_dict.get("measures", [])],
                        partitions=[SimpleNamespace(
                            name=p.get("name", ""),
                            source_type=p.get("source_type", ""),
                            source_expression=p.get("source_expression", ""),
                            query_group=p.get("query_group", ""),
                        ) for p in t_dict.get("partitions", [])],
                        lineage_tag=t_dict.get("lineage_tag", ""),
                        data_sources=t_dict.get("data_sources", []),
                    )
                    all_tables.append(table)
                for r_dict in model.get("relationships", []):
                    rel = SimpleNamespace(
                        from_table=r_dict.get("from_table", ""),
                        from_column=r_dict.get("from_column", ""),
                        to_table=r_dict.get("to_table", ""),
                        to_column=r_dict.get("to_column", ""),
                        cross_filtering=r_dict.get("cross_filtering", ""),
                        is_active=r_dict.get("is_active", True),
                    )
                    all_relationships.append(rel)
            engine.build_from_local(all_tables, all_relationships, [])
    else:
        # Fallback: API mode with minimal data
        engine.build_from_api([], [], [], {})

    return engine


def _get_or_build_engine(lineage_data: dict):
    """Cache the engine in session state."""
    if "lineage_engine" not in st.session_state:
        with st.spinner("Building lineage engine (first load)..."):
            st.session_state["lineage_engine"] = _build_engine_from_lineage(lineage_data)
    return st.session_state["lineage_engine"]


# ─── Dashboard ──────────────────────────────────────────────────────────────────
if page == "📊 Dashboard":
    st.header("Dashboard")

    # Metrics row
    col1, col2, col3, col4, col5 = st.columns(5)
    errors = [i for i in lineage["issues"] if i["severity"] == "error"]
    warnings = [i for i in lineage["issues"] if i["severity"] == "warning"]

    col1.metric("Reports", len(lineage["reports"]))
    col2.metric("Models", len(lineage["models"]))
    col3.metric("Data Sources", len(lineage["data_sources"]))
    col4.metric("Bindings", len(lineage["bindings"]))
    col5.metric("Issues", len(lineage["issues"]), delta=f"{len(errors)} errors", delta_color="inverse")

    st.markdown("---")

    # Lineage chains table
    st.subheader("Lineage Chains")
    chain_data = []
    for r in lineage["reports"]:
        model_name = r.get("bound_model") or "(unresolved)"
        model_info = next((m for m in lineage["models"] if m["name"] == model_name), None)
        chain_data.append({
            "Report": r["name"],
            "Semantic Model": model_name,
            "Tables": len(model_info["tables"]) if model_info else 0,
            "Data Sources": len(model_info["data_sources"]) if model_info else 0,
            "Status": "✅" if r.get("bound_model") else "❌ Unresolved",
        })

    st.dataframe(
        chain_data,
        use_container_width=True,
        column_config={
            "Status": st.column_config.TextColumn(width="small"),
            "Tables": st.column_config.NumberColumn(width="small"),
            "Data Sources": st.column_config.NumberColumn(width="small"),
        },
    )

    # Data sources breakdown
    st.markdown("---")
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Data Sources by Type")
        ds_types = {"SQL": 0, "File": 0, "Web": 0, "SharePoint": 0, "Other": 0}
        for ds in lineage["data_sources"]:
            if ds.startswith("SQL://"):
                ds_types["SQL"] += 1
            elif ds.startswith("File://"):
                ds_types["File"] += 1
            elif ds.startswith("Web://"):
                ds_types["Web"] += 1
            elif ds.startswith("SharePoint://"):
                ds_types["SharePoint"] += 1
            else:
                ds_types["Other"] += 1

        import pandas as pd
        ds_df = pd.DataFrame({"Type": ds_types.keys(), "Count": ds_types.values()})
        st.bar_chart(ds_df.set_index("Type"))

    with col_right:
        st.subheader("Issues Summary")
        if errors:
            st.error(f"**{len(errors)} Broken Binding(s)**")
            for e in errors[:5]:
                st.markdown(f"- {e['report']} → {e.get('expected_model', '?')}")
            if len(errors) > 5:
                st.caption(f"...and {len(errors) - 5} more")
        if warnings:
            st.warning(f"**{len(warnings)} Orphaned Model(s)**")
            for w in warnings[:5]:
                st.markdown(f"- {w.get('model', '?')}")


# ─── Lineage Graph ──────────────────────────────────────────────────────────────
elif page == "🔗 Lineage Graph":
    st.header("Interactive Lineage Graph")

    # ─── Selection Dropdown / Search ────────────────────────────────────────────
    all_reports = sorted([r["name"] for r in lineage["reports"]])
    all_models = sorted([m["name"] for m in lineage["models"]])
    all_ds = sorted(lineage["data_sources"])

    col_sel1, col_sel2, col_sel3 = st.columns(3)
    with col_sel1:
        selected_report = st.selectbox(
            "📄 Select Report",
            ["(all)"] + all_reports,
            index=0,
            key="graph_report_select",
        )
    with col_sel2:
        selected_model = st.selectbox(
            "🧊 Select Model",
            ["(all)"] + all_models,
            index=0,
            key="graph_model_select",
        )
    with col_sel3:
        selected_ds = st.selectbox(
            "🗄️ Select Data Source",
            ["(all)"] + all_ds,
            index=0,
            key="graph_ds_select",
        )

    # Determine which node is "focused" from dropdowns
    focused_node = None
    if selected_report != "(all)":
        focused_node = f"rpt_{selected_report}"
    elif selected_model != "(all)":
        focused_node = f"mdl_{selected_model}"
    elif selected_ds != "(all)":
        focused_node = f"ds_{selected_ds}"

    st.caption("Select from dropdowns above or click a node in the graph to inspect dependencies.")

    try:
        from streamlit_agraph import agraph, Node, Edge, Config

        nodes = []
        edges = []
        added_nodes = set()

        # Determine which nodes/edges to include based on focus
        # Build full edge list first for filtering
        all_edges_data = []
        for b in lineage["bindings"]:
            all_edges_data.append(("rpt_" + b["report"], "mdl_" + b["model"]))

        ds_servers = {}
        for ds in lineage["data_sources"]:
            if ds.startswith("SQL://"):
                server = ds.split("/")[2] if len(ds.split("/")) > 2 else ds
                ds_servers.setdefault(server, []).append(ds)
            else:
                ds_servers[ds] = [ds]

        for m in lineage["models"]:
            for ds in m.get("data_sources", []):
                if ds.startswith("SQL://"):
                    server = ds.split("/")[2] if len(ds.split("/")) > 2 else ds
                    target_id = f"ds_{server}"
                else:
                    target_id = f"ds_{ds}"
                all_edges_data.append((f"mdl_{m['name']}", target_id))

        # If focused, filter to only connected nodes
        if focused_node:
            connected_nodes = {focused_node}
            # Find direct connections (both directions)
            for src, tgt in all_edges_data:
                if src == focused_node or tgt == focused_node:
                    connected_nodes.add(src)
                    connected_nodes.add(tgt)
            # Second pass: find nodes connected to those (2-hop for full chain)
            first_pass = connected_nodes.copy()
            for src, tgt in all_edges_data:
                if src in first_pass or tgt in first_pass:
                    connected_nodes.add(src)
                    connected_nodes.add(tgt)
        else:
            connected_nodes = None  # Show all

        # Add report nodes
        for r in lineage["reports"]:
            node_id = f"rpt_{r['name']}"
            if connected_nodes and node_id not in connected_nodes:
                continue
            if node_id not in added_nodes:
                is_focused = node_id == focused_node
                nodes.append(Node(
                    id=node_id,
                    label=r["name"],
                    size=28 if is_focused else 20,
                    color="#1d4ed8" if is_focused else "#3b82f6",
                    shape="dot",
                    title=f"Report: {r['name']}\nPages: {len(r.get('pages', []))}",
                ))
                added_nodes.add(node_id)

        # Add model nodes
        for m in lineage["models"]:
            node_id = f"mdl_{m['name']}"
            if connected_nodes and node_id not in connected_nodes:
                continue
            if node_id not in added_nodes:
                is_focused = node_id == focused_node
                nodes.append(Node(
                    id=node_id,
                    label=m["name"],
                    size=40 if is_focused else 30,
                    color="#6d28d9" if is_focused else "#8b5cf6",
                    shape="diamond",
                    title=f"Model: {m['name']}\nTables: {len(m['tables'])}",
                ))
                added_nodes.add(node_id)

        # Add data source nodes
        for server_key in ds_servers:
            node_id = f"ds_{server_key}"
            if connected_nodes and node_id not in connected_nodes:
                continue
            if node_id not in added_nodes:
                is_focused = node_id == focused_node
                nodes.append(Node(
                    id=node_id,
                    label=server_key[:30],
                    size=22 if is_focused else 15,
                    color="#047857" if is_focused else "#10b981",
                    shape="square",
                    title=f"Data Source: {server_key}",
                ))
                added_nodes.add(node_id)

        # Add edges (only for visible nodes)
        for src, tgt in all_edges_data:
            if src in added_nodes and tgt in added_nodes:
                is_highlight = (src == focused_node or tgt == focused_node)
                edges.append(Edge(
                    source=src,
                    target=tgt,
                    color="#3b82f6" if is_highlight else "#d1d5db",
                    width=2.5 if is_highlight else 1,
                ))

        config = Config(
            width=1200,
            height=600,
            directed=True,
            physics=True,
            hierarchical=False,
            nodeHighlightBehavior=True,
            highlightColor="#f1fa8c",
            collapsible=True,
        )

        clicked_node = agraph(nodes=nodes, edges=edges, config=config)

        st.caption(f"🔵 Reports ({len(lineage['reports'])}) → 🟣 Models ({len(lineage['models'])}) → 🟢 Data Sources ({len(lineage['data_sources'])})")

        # Use clicked node or dropdown selection for detail panel
        selected_node = clicked_node or focused_node

        # ─── Dependency Detail Panel (on node click) ────────────────────────────
        if selected_node:
            st.markdown("---")
            st.subheader(f"🔍 Dependencies for: `{selected_node}`")

            # Determine what was clicked
            if selected_node.startswith("rpt_"):
                report_name = selected_node[4:]
                report_info = next((r for r in lineage["reports"] if r["name"] == report_name), None)
                if report_info:
                    st.markdown("#### 📄 Report Details")
                    col_a, col_b, col_c = st.columns(3)
                    col_a.metric("Pages", len(report_info.get("pages", [])))
                    col_b.metric("Bound Model", "✅" if report_info.get("bound_model") else "❌")
                    col_c.metric("Model ID", report_info.get("semantic_model_id", "-")[:12] + "...")

                    st.markdown(f"**Semantic Model:** {report_info.get('semantic_model_name', '-')}")
                    st.markdown(f"**Workspace:** {report_info.get('workspace', '-')}")
                    st.markdown(f"**Path:** `{report_info.get('path', '-')}`")

                    if report_info.get("pages"):
                        st.markdown("**Pages:**")
                        for pg in report_info["pages"]:
                            st.markdown(f"  - {pg}")

                    # Show downstream: model → data sources
                    model_name = report_info.get("bound_model")
                    if model_name:
                        model_info = next((m for m in lineage["models"] if m["name"] == model_name), None)
                        if model_info:
                            st.markdown("---")
                            st.markdown(f"#### ⬇️ Downstream: Model `{model_name}`")
                            col1, col2, col3 = st.columns(3)
                            col1.metric("Tables", len(model_info["tables"]))
                            col2.metric("Measures", model_info.get("total_measures", 0))
                            col3.metric("Relationships", model_info.get("relationships", 0))

                            if model_info.get("data_sources"):
                                st.markdown("**Data Sources:**")
                                for ds in model_info["data_sources"]:
                                    st.markdown(f"  - `{ds}`")

                            st.markdown("**Tables:**")
                            for t in model_info["tables"][:15]:
                                measures_text = f" ({t['measures']} measures)" if t["measures"] > 0 else ""
                                hidden = " 🙈" if t.get("is_hidden") else ""
                                st.markdown(f"  - **{t['name']}** — {t['columns']} cols{measures_text}{hidden}")

            elif selected_node.startswith("mdl_"):
                model_name = selected_node[4:]
                model_info = next((m for m in lineage["models"] if m["name"] == model_name), None)
                if model_info:
                    st.markdown("#### 🧊 Semantic Model Details")
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Tables", len(model_info["tables"]))
                    col2.metric("Measures", model_info.get("total_measures", 0))
                    col3.metric("Relationships", model_info.get("relationships", 0))
                    col4.metric("Data Sources", len(model_info.get("data_sources", [])))

                    st.markdown(f"**Path:** `{model_info.get('path', '-')}`")

                    # Upstream: reports referencing this model
                    st.markdown("---")
                    st.markdown("#### ⬆️ Upstream: Reports using this model")
                    refs = model_info.get("referenced_by", [])
                    if refs:
                        for ref in refs:
                            ref_info = next((r for r in lineage["reports"] if r["name"] == ref), None)
                            pages = len(ref_info.get("pages", [])) if ref_info else "?"
                            st.markdown(f"  - 📄 **{ref}** ({pages} pages)")
                    else:
                        st.warning("No reports reference this model (orphaned).")

                    # Downstream: data sources
                    st.markdown("---")
                    st.markdown("#### ⬇️ Downstream: Data Sources")
                    if model_info.get("data_sources"):
                        for ds in model_info["data_sources"]:
                            ds_type = classify_source(ds)
                            icon_map = {"sql": "🗄️", "file": "📁", "web": "🌐", "sp": "☁️"}
                            st.markdown(f"  - {icon_map.get(ds_type, '📦')} `{ds}`")
                    else:
                        st.info("No external data sources detected in partition expressions.")

                    # Tables with partition details
                    st.markdown("---")
                    st.markdown("#### 📊 Tables")
                    for t in model_info["tables"]:
                        measures_text = f", {t['measures']} measures" if t["measures"] > 0 else ""
                        hidden = " 🙈" if t.get("is_hidden") else ""
                        with st.expander(f"{t['name']} — {t['columns']} cols{measures_text}{hidden}"):
                            if t.get("partitions"):
                                for p in t["partitions"]:
                                    st.markdown(f"**Partition:** `{p['name']}` (type: {p['type']})")
                                    if p.get("source"):
                                        st.code(p["source"][:500], language="m")
                            if t.get("data_sources"):
                                st.markdown("**Direct data sources:**")
                                for ds in t["data_sources"]:
                                    st.markdown(f"- `{ds}`")

            elif selected_node.startswith("ds_"):
                ds_key = selected_node[3:]
                st.markdown("#### 🗄️ Data Source Details")
                st.markdown(f"**Server/Source:** `{ds_key}`")

                # Find all databases on this server
                if ds_key in ds_servers:
                    st.markdown("**Databases/paths:**")
                    for full_ds in ds_servers[ds_key]:
                        st.markdown(f"  - `{full_ds}`")

                # Find all models using this data source
                st.markdown("---")
                st.markdown("#### ⬆️ Upstream: Models consuming this source")
                consuming_models = []
                for m in lineage["models"]:
                    for ds in m.get("data_sources", []):
                        if ds_key in ds:
                            consuming_models.append(m)
                            break

                if consuming_models:
                    for m in consuming_models:
                        refs = m.get("referenced_by", [])
                        st.markdown(f"  - 🧊 **{m['name']}** → used by {len(refs)} report(s): {', '.join(refs[:5])}")
                else:
                    st.info("No models reference this data source.")

    except ImportError:
        st.warning("Install `streamlit-agraph` for interactive graph: `pip install streamlit-agraph`")
        st.markdown("Falling back to text-based lineage view:")
        st.markdown("---")

        for b in lineage["bindings"]:
            model_info = next((m for m in lineage["models"] if m["name"] == b["model"]), None)
            sources = ", ".join(model_info["data_sources"][:3]) if model_info else "?"
            st.markdown(f"📄 **{b['report']}** → 🧊 {b['model']} → 🗄️ {sources}")


# ─── ERD Diagram ────────────────────────────────────────────────────────────────
elif page == "🗺️ ERD Diagram":
    st.header("🗺️ Entity-Relationship Diagram")

    try:
        engine = _get_or_build_engine(lineage)
    except Exception as e:
        st.error(f"Failed to build engine: {e}")
        import traceback
        st.code(traceback.format_exc())
        st.stop()
    stats = engine.get_stats()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Tables", stats["nodes_by_type"].get("table", 0))
    col2.metric("Columns", stats["nodes_by_type"].get("column", 0))
    col3.metric("Measures", stats["nodes_by_type"].get("measure", 0))
    col4.metric("Relationships", stats["edges_by_type"].get("has_relationship", 0) // 2)

    st.markdown("---")

    show_cols = st.checkbox("Show columns in diagram", value=True)
    max_cols = st.slider("Max columns per table", 3, 20, 8)

    # Filter tables to avoid overwhelming Mermaid
    all_tables = sorted([n.name for n in engine.nodes.values() if n.type == "table"])
    max_tables = st.slider("Max tables to display", 5, min(len(all_tables), 100), min(30, len(all_tables)))
    selected_tables = st.multiselect("Filter tables (leave empty for top N)", all_tables)

    from diagram_renderer import generate_erd_mermaid
    filter_set = set(selected_tables) if selected_tables else set(all_tables[:max_tables])
    mermaid_code = generate_erd_mermaid(engine, show_columns=show_cols, max_columns=max_cols, table_filter=filter_set)

    if stats["nodes_by_type"].get("table", 0) > 0:
        import streamlit.components.v1 as components
        mermaid_html = f"""
        <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
        <div class="mermaid" style="background: white; padding: 20px; border-radius: 8px;">
{mermaid_code}
        </div>
        <script>mermaid.initialize({{startOnLoad: true, theme: 'default', maxTextSize: 500000, er: {{useMaxWidth: true}}}});</script>
        """
        components.html(mermaid_html, height=600, scrolling=True)

        with st.expander("📋 Mermaid Source (copy for docs)"):
            st.code(mermaid_code, language="mermaid")

        st.markdown("---")
        st.subheader("Relationships")
        from model_detail import get_relationship_matrix
        rels = get_relationship_matrix(engine)
        if rels:
            st.dataframe(rels, use_container_width=True)
    else:
        st.info("No table data available. Load a model to see the ERD.")


# ─── Lineage Flow ───────────────────────────────────────────────────────────────
elif page == "🌊 Lineage Flow":
    st.header("🌊 Full Lineage Flow")
    st.caption("Data Source → Table → Measure → Visual (left to right)")

    try:
        engine = _get_or_build_engine(lineage)
    except Exception as e:
        st.error(f"Failed to build engine: {e}")
        import traceback
        st.code(traceback.format_exc())
        st.stop()
    stats = engine.get_stats()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Sources", stats["nodes_by_type"].get("dataSource", 0))
    col2.metric("Tables", stats["nodes_by_type"].get("table", 0))
    col3.metric("Columns", stats["nodes_by_type"].get("column", 0))
    col4.metric("Measures", stats["nodes_by_type"].get("measure", 0))
    col5.metric("Visuals", stats["nodes_by_type"].get("visual", 0))

    st.markdown("---")

    view_mode = st.radio("View Mode", ["Interactive Graph (vis.js)", "Mermaid Flowchart"], horizontal=True)

    # Node type filter — columns are 2252 of 3854 nodes, too many by default
    st.markdown("**Node types to display:**")
    ft_col1, ft_col2, ft_col3, ft_col4, ft_col5 = st.columns(5)
    show_sources = ft_col1.checkbox("Sources", value=True, key="flow_show_src")
    show_tables = ft_col2.checkbox("Tables", value=True, key="flow_show_tbl")
    show_columns = ft_col3.checkbox("Columns", value=False, key="flow_show_col")
    show_measures = ft_col4.checkbox("Measures", value=True, key="flow_show_msr")
    show_visuals = ft_col5.checkbox("Visuals", value=True, key="flow_show_vis")
    visible_types = set()
    if show_sources:
        visible_types.add("dataSource")
    if show_tables:
        visible_types.add("table")
    if show_columns:
        visible_types.add("column")
    if show_measures:
        visible_types.add("measure")
    if show_visuals:
        visible_types.add("visual")

    max_nodes = st.slider("Max nodes to display", 10, 500, 150, key="flow_max_nodes")

    # Focus node selector — only show nodes of visible types
    filtered_node_names = sorted([
        f"{n.type}: {n.name}" for n in engine.nodes.values()
        if n.type in visible_types
    ])
    focus_selection = st.selectbox("Focus on node (optional)", ["(show all)"] + filtered_node_names)

    focus_id = None
    if focus_selection != "(show all)":
        for nid, node in engine.nodes.items():
            if f"{node.type}: {node.name}" == focus_selection:
                focus_id = nid
                break

    if view_mode == "Interactive Graph (vis.js)":
        from diagram_renderer import generate_pyvis_html
        import streamlit.components.v1 as components

        html = generate_pyvis_html(engine, focus_node=focus_id, height="650px",
                                   visible_types=visible_types, max_nodes=max_nodes)
        components.html(html, height=700, scrolling=True)
    else:
        from diagram_renderer import generate_lineage_mermaid
        import streamlit.components.v1 as components

        mermaid_code = generate_lineage_mermaid(engine, focus_node=focus_id,
                                                visible_types=visible_types, max_nodes=max_nodes)
        mermaid_html = f"""
        <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
        <div class="mermaid" style="background: white; padding: 20px; border-radius: 8px; overflow-x: auto;">
{mermaid_code}
        </div>
        <script>mermaid.initialize({{startOnLoad: true, theme: 'default', maxTextSize: 500000, flowchart: {{useMaxWidth: false, curve: 'basis'}}}});</script>
        """
        components.html(mermaid_html, height=600, scrolling=True)

        with st.expander("📋 Mermaid Source"):
            st.code(mermaid_code, language="mermaid")


# ─── Impact Analysis ────────────────────────────────────────────────────────────
elif page == "🎯 Impact Analysis":
    st.header("🎯 Impact Analysis")
    st.caption("Select any object to see what is affected if it changes.")

    try:
        engine = _get_or_build_engine(lineage)
    except Exception as e:
        st.error(f"Failed to build engine: {e}")
        import traceback
        st.code(traceback.format_exc())
        st.stop()

    tables = sorted([n.name for n in engine.nodes.values() if n.type == "table"])
    columns_by_table = {}
    for n in engine.nodes.values():
        if n.type == "column":
            columns_by_table.setdefault(n.table, []).append(n.name)
    measures = sorted([f"{n.table}.{n.name}" for n in engine.nodes.values() if n.type == "measure"])
    sources = sorted([n.name for n in engine.nodes.values() if n.type == "dataSource"])

    col_type, col_select = st.columns([1, 3])
    with col_type:
        node_type = st.selectbox("Object type", ["Table", "Column", "Measure", "Data Source"])

    with col_select:
        if node_type == "Table":
            selected = st.selectbox("Select table", tables)
            node_id = f"table:{selected}" if selected else None
        elif node_type == "Column":
            sel_table = st.selectbox("Table", tables, key="impact_table")
            cols = sorted(columns_by_table.get(sel_table, []))
            sel_col = st.selectbox("Column", cols)
            node_id = f"column:{sel_table}.{sel_col}" if sel_col else None
        elif node_type == "Measure":
            sel_measure = st.selectbox("Select measure", measures)
            if sel_measure:
                parts = sel_measure.split(".", 1)
                node_id = f"measure:{parts[0]}.{parts[1]}" if len(parts) == 2 else None
            else:
                node_id = None
        else:
            sel_source = st.selectbox("Select data source", sources)
            node_id = None
            for nid, n in engine.nodes.items():
                if n.type == "dataSource" and n.name == sel_source:
                    node_id = nid
                    break

    if node_id and node_id in engine.nodes:
        st.markdown("---")
        impact = engine.get_impact(node_id)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Impact", impact["total_impact"])
        c2.metric("Affected Measures", len(impact["affected_measures"]))
        c3.metric("Affected Visuals", len(impact["affected_visuals"]))
        c4.metric("Affected Tables", len(impact["affected_tables"]))

        from diagram_renderer import generate_impact_mermaid
        import streamlit.components.v1 as components

        mermaid_code = generate_impact_mermaid(engine, node_id)
        mermaid_html = f"""
        <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
        <div class="mermaid" style="background: white; padding: 20px; border-radius: 8px;">
{mermaid_code}
        </div>
        <script>mermaid.initialize({{startOnLoad: true, theme: 'default', maxTextSize: 500000}});</script>
        """
        components.html(mermaid_html, height=400, scrolling=True)

        if impact["affected_measures"]:
            st.subheader("Affected Measures")
            for m in impact["affected_measures"]:
                st.markdown(f"- `[{m.name}]` (table: *{m.table}*)")

        if impact["affected_visuals"]:
            st.subheader("Affected Visuals")
            for v in impact["affected_visuals"]:
                page = v.detail.get("page", "")
                st.markdown(f"- **{v.name}** ({v.detail.get('visual_type', '')}) on page *{page}*")

        st.markdown("---")
        st.subheader("Upstream Dependencies")
        upstream = engine.get_upstream(node_id)
        if upstream:
            for u in upstream:
                st.markdown(f"- [{u.type}] **{u.name}**" + (f" (table: {u.table})" if u.table else ""))
        else:
            st.info("No upstream dependencies.")
    else:
        st.info("Select an object above to see its impact.")


# ─── Model Explorer ─────────────────────────────────────────────────────────────
elif page == "📐 Model Explorer":
    st.header("📐 Model Explorer — Tables & Columns")
    st.caption("Deep-dive into table structure, column usage, and measure catalog.")

    try:
        engine = _get_or_build_engine(lineage)
    except Exception as e:
        st.error(f"Failed to build engine: {e}")
        import traceback
        st.code(traceback.format_exc())
        st.stop()

    from model_detail import (
        get_table_inventory,
        get_column_detail,
        get_measure_catalog,
        get_relationship_matrix,
        get_data_source_inventory,
    )

    tab1, tab2, tab3, tab4 = st.tabs(["📊 Table Inventory", "📏 Column Explorer", "📐 Measure Catalog", "🗄️ Data Sources"])

    with tab1:
        inventory = get_table_inventory(engine)
        if inventory:
            st.dataframe(
                inventory,
                use_container_width=True,
                column_config={
                    "name": st.column_config.TextColumn("Table Name", width="large"),
                    "columns": st.column_config.NumberColumn("Columns", width="small"),
                    "measures": st.column_config.NumberColumn("Measures", width="small"),
                    "relationships": st.column_config.NumberColumn("Relationships", width="small"),
                    "data_sources": st.column_config.NumberColumn("Sources", width="small"),
                    "visual_consumers": st.column_config.NumberColumn("Visual Users", width="small"),
                    "is_hidden": st.column_config.CheckboxColumn("Hidden", width="small"),
                },
            )

            unused = engine.get_unused_columns()
            if unused:
                st.warning(f"⚠️ {len(unused)} unused columns detected (not referenced by any measure, visual, or relationship)")
                with st.expander(f"Show {len(unused)} unused columns"):
                    for col in unused[:50]:
                        st.markdown(f"- `{col.table}.[{col.name}]` ({col.detail.get('data_type', '')})")
        else:
            st.info("No table data available.")

    with tab2:
        tables = sorted([n.name for n in engine.nodes.values() if n.type == "table"])
        selected_table = st.selectbox("Select table", tables, key="explorer_table")

        if selected_table:
            columns = get_column_detail(engine, selected_table)
            if columns:
                used_count = sum(1 for c in columns if c["is_used"])
                c1, c2, c3 = st.columns(3)
                c1.metric("Total Columns", len(columns))
                c2.metric("Used Columns", used_count)
                c3.metric("Unused", len(columns) - used_count)

                display_data = []
                for col in columns:
                    display_data.append({
                        "Column": col["name"],
                        "Type": col["data_type"],
                        "Hidden": col["is_hidden"],
                        "Relationships": ", ".join(col["relationships"]) if col["relationships"] else "-",
                        "Used by Measures": len(col["used_by_measures"]),
                        "Used by Visuals": len(col["used_by_visuals"]),
                        "Status": "✅ Used" if col["is_used"] else "⚠️ Unused",
                    })
                st.dataframe(display_data, use_container_width=True)

                col_names = [c["name"] for c in columns]
                sel_col = st.selectbox("Inspect column", col_names, key="col_detail")
                col_info = next((c for c in columns if c["name"] == sel_col), None)
                if col_info:
                    st.markdown(f"**Data type:** `{col_info['data_type']}`")
                    if col_info["relationships"]:
                        st.markdown("**Relationships:** " + ", ".join(f"`{r}`" for r in col_info["relationships"]))
                    if col_info["used_by_measures"]:
                        st.markdown("**Referenced by measures:** " + ", ".join(f"`[{m}]`" for m in col_info["used_by_measures"]))
                    if col_info["used_by_visuals"]:
                        st.markdown("**Used by visuals:** " + ", ".join(col_info["used_by_visuals"]))

    with tab3:
        tables_for_measures = sorted([n.name for n in engine.nodes.values() if n.type == "table"])
        filter_table = st.selectbox("Filter by table", ["(all)"] + tables_for_measures, key="measure_table_filter")

        catalog = get_measure_catalog(engine, table_name=filter_table if filter_table != "(all)" else None)
        if catalog:
            c1, c2, c3 = st.columns(3)
            c1.metric("Total Measures", len(catalog))
            c2.metric("With Dependencies", sum(1 for m in catalog if m["depends_on"]))
            c3.metric("Used by Visuals", sum(1 for m in catalog if m["visual_users"]))

            for m in catalog:
                deps_badge = f"⬆️{len(m['depends_on'])}" if m["depends_on"] else ""
                impact_badge = f"⬇️{len(m['depended_by'])}" if m["depended_by"] else ""
                visual_badge = f"👁️{len(m['visual_users'])}" if m["visual_users"] else ""
                badges = " ".join(filter(None, [deps_badge, impact_badge, visual_badge]))

                with st.expander(f"**[{m['name']}]** ({m['table']}) {badges}"):
                    st.code(m["expression"], language="dax")
                    col_a, col_b = st.columns(2)
                    with col_a:
                        if m["depends_on"]:
                            st.markdown("**Depends on:** " + ", ".join(f"`[{d}]`" for d in m["depends_on"]))
                        if m["column_refs"]:
                            st.markdown("**Column refs:** " + ", ".join(f"`{t}[{c}]`" for t, c in m["column_refs"]))
                    with col_b:
                        if m["depended_by"]:
                            st.markdown("**Depended by:** " + ", ".join(f"`[{d}]`" for d in m["depended_by"]))
                        if m["visual_users"]:
                            st.markdown("**Visual users:** " + ", ".join(m["visual_users"]))

                    complexity = m["complexity"]
                    if complexity >= 10:
                        st.caption(f"🔴 High complexity ({complexity})")
                    elif complexity >= 5:
                        st.caption(f"🟡 Medium complexity ({complexity})")
                    else:
                        st.caption(f"🟢 Low complexity ({complexity})")
        else:
            st.info("No measures found.")

    with tab4:
        ds_inventory = get_data_source_inventory(engine)
        if ds_inventory:
            for ds in ds_inventory:
                icon = {"sql_server": "🗄️", "sharepoint_tables": "☁️", "web": "🌐", "excel": "📊"}.get(ds["type"], "📦")
                with st.expander(f"{icon} {ds['name']} ({ds['table_count']} tables)"):
                    st.markdown(f"**Type:** `{ds['type']}`")
                    if ds["server"]:
                        st.markdown(f"**Server:** `{ds['server']}`")
                    if ds["database"]:
                        st.markdown(f"**Database:** `{ds['database']}`")
                    if ds["url"]:
                        st.markdown(f"**URL:** `{ds['url']}`")
                    st.markdown("**Consuming tables:** " + ", ".join(f"`{t}`" for t in ds["tables"]))
        else:
            st.info("No data sources detected in partition expressions.")

    st.markdown("---")
    broken = engine.get_broken_references()
    if broken:
        st.subheader(f"⚠️ Broken References ({len(broken)})")
        for b in broken[:20]:
            st.markdown(f"- **{b['from_node']}** ({b['from_type']}) → missing `{b['missing_target']}`")
    else:
        st.success("✅ No broken references detected.")


# ─── Model Insights View ────────────────────────────────────────────────────────
elif page == "🧬 Model Insights":
    st.header("🧬 Model Insights — DAX Dependencies & Data Sources")

    if lineage and lineage.get("_models_raw"):
        models_raw = lineage["_models_raw"]
        engine = _get_or_build_engine(lineage)
        model_names = [_get(m, "name", "") for m in models_raw]
        selected_model_name = st.selectbox("Select Semantic Model", model_names)
        selected_model = next((m for m in models_raw if _get(m, "name") == selected_model_name), None)

        if selected_model:
            model_path = _get(selected_model, "path", "")

            # Run enhanced scan only if path is valid (local mode)
            enhanced = None
            if model_path and Path(str(model_path)).exists():
                @st.cache_data
                def _enhanced_model_scan(path_str):
                    return scan_semantic_model_enhanced(Path(path_str))
                enhanced = _enhanced_model_scan(str(model_path))

            # Build dep_graph from enhanced scan or from engine
            dep_graph = enhanced.get("measure_dependencies", {}) if enhanced else {}
            if not dep_graph:
                dep_graph = engine.dep_graph
                # Filter to measures belonging to this model's tables
                model_tables = {_get(t, "name") for t in _get(selected_model, "tables", [])}
                dep_graph = {k: v for k, v in dep_graph.items() if v.get("table") in model_tables}

            tab1, tab2, tab3, tab4 = st.tabs(["📐 DAX Dependencies", "🗄️ Data Sources", "🔐 Roles (RLS)", "📖 Expressions"])

            # --- DAX Dependencies Tab ---
            with tab1:
                st.subheader("Measure Dependency Graph")
                if dep_graph:
                    # Measure selector
                    measure_list = sorted(dep_graph.keys())
                    selected_measure = st.selectbox("Select measure to analyze", measure_list)

                    if selected_measure:
                        info = dep_graph[selected_measure]
                        col1, col2 = st.columns(2)

                        with col1:
                            st.markdown("**Dependencies (what this measure uses):**")
                            chain = resolve_measure_chain(selected_measure, dep_graph)
                            if chain:
                                for dep in chain:
                                    dep_table = dep_graph.get(dep, {}).get("table", "?")
                                    st.markdown(f"  - `[{dep}]` (in *{dep_table}*)")
                            else:
                                st.info("No measure dependencies (leaf measure)")

                            if info["depends_on_columns"]:
                                st.markdown("**Column references:**")
                                for tbl, col in info["depends_on_columns"]:
                                    st.markdown(f"  - `'{tbl}'[{col}]`")

                            if info["depends_on_tables"]:
                                st.markdown("**Table references:**")
                                for tbl in info["depends_on_tables"]:
                                    st.markdown(f"  - `{tbl}`")

                        with col2:
                            st.markdown("**Impact (measures that depend on this):**")
                            impacted = get_measure_impact(selected_measure, dep_graph)
                            if impacted:
                                for imp in impacted:
                                    imp_table = dep_graph.get(imp, {}).get("table", "?")
                                    st.markdown(f"  - `[{imp}]` (in *{imp_table}*)")
                            else:
                                st.info("No downstream dependents")

                        # Show DAX expression
                        with st.expander("DAX Expression"):
                            st.code(info.get("expression", ""), language="dax")

                    # Summary stats
                    st.markdown("---")
                    total_measures = len(dep_graph)
                    leaf_measures = sum(1 for m in dep_graph.values() if not m["depends_on_measures"])
                    complex_measures = sum(1 for m in dep_graph.values() if len(m["depends_on_measures"]) >= 3)
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Total Measures", total_measures)
                    c2.metric("Leaf Measures", leaf_measures)
                    c3.metric("Complex (3+ deps)", complex_measures)
                else:
                    st.info("No measures found in this model.")

            # --- Data Sources Tab ---
            with tab2:
                st.subheader("Data Sources (Comprehensive)")
                data_sources = enhanced.get("data_sources", []) if enhanced else []
                if data_sources:
                    # Group by type
                    by_type = {}
                    for ds in data_sources:
                        by_type.setdefault(ds.source_type, []).append(ds)

                    for src_type, sources in sorted(by_type.items()):
                        gateway_icon = "☁️" if sources[0].gateway_required == "cloud" else "🏢"
                        st.markdown(f"**{gateway_icon} {src_type}** ({len(sources)} source{'s' if len(sources) > 1 else ''})")
                        for ds in sources:
                            detail = ds.server or ds.url or ds.path or "(embedded)"
                            param_tag = " 🏷️ *parameterized*" if ds.parameterized else ""
                            st.markdown(f"  - `{detail}`{param_tag}")
                else:
                    st.info("No data sources detected.")

            # --- Roles Tab ---
            with tab3:
                st.subheader("Row-Level Security Roles")
                roles = enhanced.get("roles", []) if enhanced else []
                if roles:
                    for role in roles:
                        with st.expander(f"🔐 {role.name} ({len(role.table_permissions)} table permissions)"):
                            st.markdown(f"**Model Permission:** {role.model_permission}")
                            for perm in role.table_permissions:
                                st.markdown(f"**Table:** `{perm.table}`")
                                if perm.filter_expression:
                                    st.code(perm.filter_expression, language="dax")
                else:
                    st.info("No RLS roles defined.")

            # --- Expressions Tab ---
            with tab4:
                st.subheader("Named Expressions & Parameters")
                expressions = enhanced.get("expressions", []) if enhanced else []
                if expressions:
                    params = [e for e in expressions if e.is_parameter]
                    queries = [e for e in expressions if not e.is_parameter]

                    if params:
                        st.markdown("**Parameters:**")
                        for p in params:
                            st.markdown(f"  - 🏷️ `{p.name}`")
                            if p.expression:
                                st.code(p.expression[:200], language="m")

                    if queries:
                        st.markdown("**Shared Queries:**")
                        for q in queries:
                            with st.expander(f"📝 {q.name}"):
                                st.code(q.expression[:500], language="m")
                else:
                    st.info("No named expressions found.")

            # Enhanced relationships
            st.markdown("---")
            st.subheader("Relationships")
            rels = enhanced.get("relationships", []) if enhanced else []
            if rels:
                rel_data = []
                for r in rels:
                    card = f"{r.from_cardinality}:{r.to_cardinality}"
                    cross = "↔" if "both" in r.cross_filtering.lower() else "→"
                    active = "✓" if r.is_active else "✗"
                    rel_data.append({
                        "From": f"{r.from_table}[{r.from_column}]",
                        "To": f"{r.to_table}[{r.to_column}]",
                        "Cardinality": card,
                        "Filter": cross,
                        "Active": active,
                    })
                st.dataframe(rel_data, use_container_width=True)
            else:
                # Fallback: show relationships from dict-based _models_raw
                model_rels = _get(selected_model, "relationships", [])
                if model_rels:
                    rel_data = []
                    for r in model_rels:
                        r_from_table = _get(r, "from_table", "")
                        r_from_col = _get(r, "from_column", "")
                        r_to_table = _get(r, "to_table", "")
                        r_to_col = _get(r, "to_column", "")
                        cross = _get(r, "cross_filtering", "")
                        active = "✓" if _get(r, "is_active", True) else "✗"
                        rel_data.append({
                            "From": f"{r_from_table}[{r_from_col}]",
                            "To": f"{r_to_table}[{r_to_col}]",
                            "Filter": cross,
                            "Active": active,
                        })
                    st.dataframe(rel_data, use_container_width=True)
                else:
                    st.info("No relationships found.")
    else:
        st.warning("No data loaded. Use the sidebar to scan or load lineage data.")


# ─── Report Insights View ───────────────────────────────────────────────────────
elif page == "📑 Report Insights":
    st.header("📑 Report Insights — Visual Field Usage")

    if lineage and lineage.get("_reports_raw"):
        reports_raw = lineage["_reports_raw"]
        report_names = [r.name for r in reports_raw]
        selected_report_name = st.selectbox("Select Report", report_names)
        selected_report = next((r for r in reports_raw if r.name == selected_report_name), None)

        if selected_report:
            @st.cache_data
            def _enhanced_report_scan(path_str):
                return scan_report_enhanced(Path(path_str))

            enhanced_rpt = _enhanced_report_scan(str(selected_report.path))

            # Summary metrics
            c1, c2, c3 = st.columns(3)
            c1.metric("Pages", enhanced_rpt.get("page_count", 0))
            c2.metric("Visuals", enhanced_rpt.get("visual_count", 0))
            c3.metric("Unique Fields Used", len(enhanced_rpt.get("field_usage_map", {})))

            tab1, tab2, tab3 = st.tabs(["📄 Pages & Visuals", "📊 Field Usage", "🔍 Unused Fields"])

            # --- Pages & Visuals Tab ---
            with tab1:
                pages = enhanced_rpt.get("pages", [])
                for pg in pages:
                    with st.expander(f"📄 {pg.display_name or pg.name} ({len(pg.visuals)} visuals)"):
                        if pg.visuals:
                            visual_types = {}
                            for v in pg.visuals:
                                visual_types[v.visual_type] = visual_types.get(v.visual_type, 0) + 1

                            st.markdown("**Visual types:** " + ", ".join(f"{t} ({c})" for t, c in sorted(visual_types.items())))
                            st.markdown("---")
                            for v in pg.visuals:
                                fields_str = ", ".join(f"`{f[1]}.{f[2]}`" for f in v.fields[:5])
                                more = f" +{len(v.fields)-5} more" if len(v.fields) > 5 else ""
                                st.markdown(f"  - **{v.name}** ({v.visual_type}): {fields_str}{more}")
                        else:
                            st.info("No visuals on this page.")

            # --- Field Usage Tab ---
            with tab2:
                st.subheader("Field Usage Across Visuals")
                usage_map = enhanced_rpt.get("field_usage_map", {})
                if usage_map:
                    # Sort by most used
                    sorted_fields = sorted(usage_map.items(), key=lambda x: len(x[1]), reverse=True)
                    usage_data = []
                    for key, usages in sorted_fields[:50]:
                        parts = key.split("|")
                        field_type, table, field_name = parts[0], parts[1], parts[2]
                        visual_names = list(set(u.visual_name for u in usages))
                        usage_data.append({
                            "Type": field_type,
                            "Table": table,
                            "Field": field_name,
                            "Used In": len(usages),
                            "Visuals": ", ".join(visual_names[:3]) + ("..." if len(visual_names) > 3 else ""),
                        })
                    st.dataframe(usage_data, use_container_width=True)
                else:
                    st.info("No field usage data extracted.")

            # --- Unused Fields Tab ---
            with tab3:
                st.subheader("Potentially Unused Fields")
                st.markdown("*Fields in the semantic model NOT referenced by any visual in this report.*")

                # Get model fields from linked model
                model_name = enhanced_rpt.get("semantic_model_name", "")
                usage_map = enhanced_rpt.get("field_usage_map", {})
                used_fields = set()
                for key in usage_map:
                    parts = key.split("|")
                    used_fields.add((parts[1], parts[2]))

                # Find the model
                if lineage.get("_models_raw"):
                    linked_model = next((m for m in lineage["_models_raw"] if _get(m, "name") == model_name), None)
                    if linked_model:
                        unused = []
                        for table in _get(linked_model, "tables", []):
                            if _get(table, "is_hidden", False):
                                continue
                            for col in _get(table, "columns", []):
                                if not _get(col, "is_hidden", False) and (_get(table, "name"), _get(col, "name")) not in used_fields:
                                    unused.append({"Table": _get(table, "name"), "Column": _get(col, "name")})
                        if unused:
                            st.dataframe(unused[:100], use_container_width=True)
                            st.caption(f"Showing {min(len(unused), 100)} of {len(unused)} unused fields")
                        else:
                            st.success("All visible fields are used!")
                    else:
                        st.info(f"Linked model '{model_name}' not found in scan.")
                else:
                    st.info("No model data available for cross-reference.")
    else:
        st.warning("No data loaded. Use the sidebar to scan or load lineage data.")


# ─── Reports View ───────────────────────────────────────────────────────────────
elif page == "📋 Reports":
    st.header("Reports")

    # Filters
    col1, col2 = st.columns(2)
    with col1:
        status_filter = st.selectbox("Status", ["All", "Bound", "Unresolved"])
    with col2:
        search = st.text_input("Search reports", placeholder="Type to filter...")

    reports_filtered = lineage["reports"]
    if status_filter == "Bound":
        reports_filtered = [r for r in reports_filtered if r.get("bound_model")]
    elif status_filter == "Unresolved":
        reports_filtered = [r for r in reports_filtered if not r.get("bound_model")]
    if search:
        reports_filtered = [r for r in reports_filtered if search.lower() in r["name"].lower()]

    st.caption(f"Showing {len(reports_filtered)} of {len(lineage['reports'])} reports")

    for r in reports_filtered:
        status = "✅" if r.get("bound_model") else "❌"
        with st.expander(f"{status} {r['name']}"):
            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown(f"**Semantic Model:** {r.get('semantic_model_name', '-')}")
                st.markdown(f"**Model ID:** `{r.get('semantic_model_id', '-')}`")
                st.markdown(f"**Workspace:** {r.get('workspace', '-')}")
            with col_b:
                st.markdown(f"**Bound Model:** {r.get('bound_model', 'None (unresolved)')}")
                st.markdown(f"**Pages:** {len(r.get('pages', []))}")
                if r.get("pages"):
                    st.markdown(f"**Page list:** {', '.join(r['pages'][:10])}")
            st.markdown(f"**Path:** `{r.get('path', '-')}`")


# ─── Models View ────────────────────────────────────────────────────────────────
elif page == "🧊 Models":
    st.header("Semantic Models")

    search = st.text_input("Search models", placeholder="Type to filter...")
    models_filtered = lineage["models"]
    if search:
        models_filtered = [m for m in models_filtered if search.lower() in m["name"].lower()]

    st.caption(f"Showing {len(models_filtered)} of {len(lineage['models'])} models")

    for m in models_filtered:
        ref_count = len(m.get("referenced_by", []))
        icon = "🟢" if ref_count > 0 else "🟡"
        with st.expander(f"{icon} {m['name']} — {len(m['tables'])} tables, {m.get('total_measures', 0)} measures"):
            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown(f"**Tables:** {len(m['tables'])}")
                st.markdown(f"**Relationships:** {m.get('relationships', 0)}")
                st.markdown(f"**Total Measures:** {m.get('total_measures', 0)}")
            with col_b:
                st.markdown(f"**Referenced by:** {', '.join(m.get('referenced_by', [])) or 'None (orphaned)'}")
                st.markdown(f"**Data Sources:** {len(m.get('data_sources', []))}")

            # Data sources
            if m.get("data_sources"):
                st.markdown("**Data Sources:**")
                sources_html = " ".join(source_badge(ds) for ds in m["data_sources"])
                st.markdown(sources_html, unsafe_allow_html=True)

            # Tables detail
            if m.get("tables"):
                st.markdown("**Tables:**")
                table_data = []
                for t in m["tables"]:
                    table_data.append({
                        "Table": t["name"],
                        "Columns": t["columns"],
                        "Measures": t["measures"],
                        "Hidden": "🙈" if t.get("is_hidden") else "",
                        "Sources": len(t.get("data_sources", [])),
                    })
                st.dataframe(table_data, use_container_width=True, hide_index=True)


# ─── Issues View ────────────────────────────────────────────────────────────────
elif page == "⚠️ Issues":
    st.header("Issues")

    issues = lineage["issues"]
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Issues", len(issues))
    col2.metric("Errors", len(errors))
    col3.metric("Warnings", len(warnings))

    st.markdown("---")

    severity_filter = st.selectbox("Filter by severity", ["All", "Errors only", "Warnings only"])

    if severity_filter == "Errors only":
        display_issues = errors
    elif severity_filter == "Warnings only":
        display_issues = warnings
    else:
        display_issues = issues

    for issue in display_issues:
        css_class = "issue-error" if issue["severity"] == "error" else "issue-warning"
        icon = "❌" if issue["severity"] == "error" else "⚠️"
        st.markdown(
            f'<div class="{css_class}">{icon} <strong>{issue["type"]}</strong>: {issue["message"]}</div>',
            unsafe_allow_html=True,
        )

    if not display_issues:
        st.success("No issues found! All bindings are healthy.")


# ─── Search View ────────────────────────────────────────────────────────────────
elif page == "🔎 Search":
    st.header("Search")

    query = st.text_input("Search across all artifacts", placeholder="e.g. SQL03, KDK, Elevtal, table name...")

    if query:
        q = query.lower()
        results = {"reports": [], "models": [], "data_sources": [], "issues": [], "tables": []}

        for r in lineage["reports"]:
            if q in r["name"].lower() or q in str(r.get("semantic_model_name", "")).lower() or q in str(r.get("path", "")).lower():
                results["reports"].append(r)

        for m in lineage["models"]:
            if q in m["name"].lower() or q in str(m.get("path", "")).lower():
                results["models"].append(m)
            # Also search inside tables
            for t in m.get("tables", []):
                if q in t["name"].lower():
                    results["tables"].append({"table": t, "model": m["name"]})

        for ds in lineage["data_sources"]:
            if q in ds.lower():
                results["data_sources"].append(ds)

        for i in lineage["issues"]:
            if q in i.get("message", "").lower():
                results["issues"].append(i)

        total = sum(len(v) for v in results.values())
        st.caption(f"Found {total} result(s)")

        # ─── Reports with full PBIP detail ──────────────────────────────────────
        if results["reports"]:
            st.subheader(f"📄 Reports ({len(results['reports'])})")
            for r in results["reports"]:
                status = "✅" if r.get("bound_model") else "❌"
                with st.expander(f"{status} {r['name']}", expanded=len(results['reports']) == 1):
                    col_a, col_b, col_c = st.columns(3)
                    with col_a:
                        st.markdown("**PBIP Report Info**")
                        st.markdown(f"- **Name:** {r['name']}")
                        st.markdown(f"- **Path:** `{r.get('path', '-')}`")
                        st.markdown(f"- **Pages:** {len(r.get('pages', []))}")
                    with col_b:
                        st.markdown("**Semantic Model Binding**")
                        st.markdown(f"- **Model Name:** {r.get('semantic_model_name', '-')}")
                        st.markdown(f"- **Model ID:** `{r.get('semantic_model_id', '-')}`")
                        st.markdown(f"- **Workspace:** {r.get('workspace', '-')}")
                    with col_c:
                        st.markdown("**Status**")
                        st.markdown(f"- **Binding:** {'Resolved' if r.get('bound_model') else 'BROKEN'}")
                        st.markdown(f"- **Bound to:** {r.get('bound_model', 'None')}")

                    # Show pages
                    if r.get("pages"):
                        st.markdown("**Report Pages:**")
                        page_cols = st.columns(min(4, len(r["pages"])))
                        for idx, pg in enumerate(r["pages"]):
                            page_cols[idx % 4].markdown(f"📑 {pg}")

                    # Show full downstream lineage
                    model_name = r.get("bound_model")
                    if model_name:
                        model_info = next((m for m in lineage["models"] if m["name"] == model_name), None)
                        if model_info:
                            st.markdown("---")
                            st.markdown(f"**⬇️ Full Lineage Chain:**")
                            st.markdown(f"📄 `{r['name']}` → 🧊 `{model_name}` ({len(model_info['tables'])} tables, {model_info.get('total_measures', 0)} measures)")
                            if model_info.get("data_sources"):
                                for ds in model_info["data_sources"]:
                                    st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;→ 🗄️ `{ds}`")

        # ─── Models with full PBIP detail ───────────────────────────────────────
        if results["models"]:
            st.subheader(f"🧊 Semantic Models ({len(results['models'])})")
            for m in results["models"]:
                ref_count = len(m.get("referenced_by", []))
                icon = "🟢" if ref_count > 0 else "🟡"
                with st.expander(f"{icon} {m['name']}", expanded=len(results['models']) == 1):
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.markdown("**PBIP Semantic Model Info**")
                        st.markdown(f"- **Name:** {m['name']}")
                        st.markdown(f"- **Path:** `{m.get('path', '-')}`")
                        st.markdown(f"- **Tables:** {len(m['tables'])}")
                        st.markdown(f"- **Relationships:** {m.get('relationships', 0)}")
                        st.markdown(f"- **Total Measures:** {m.get('total_measures', 0)}")
                    with col_b:
                        st.markdown("**Connections**")
                        st.markdown(f"- **Referenced by:** {len(m.get('referenced_by', []))} report(s)")
                        if m.get("referenced_by"):
                            for ref in m["referenced_by"]:
                                st.markdown(f"  - 📄 {ref}")
                        st.markdown(f"- **Data Sources:** {len(m.get('data_sources', []))}")
                        if m.get("data_sources"):
                            for ds in m["data_sources"]:
                                ds_type = classify_source(ds)
                                icon_map = {"sql": "🗄️", "file": "📁", "web": "🌐", "sp": "☁️"}
                                st.markdown(f"  - {icon_map.get(ds_type, '📦')} `{ds}`")

                    # Full table listing
                    st.markdown("---")
                    st.markdown("**Tables in this model:**")
                    table_data = []
                    for t in m["tables"]:
                        table_data.append({
                            "Table": t["name"],
                            "Columns": t["columns"],
                            "Measures": t["measures"],
                            "Hidden": "🙈" if t.get("is_hidden") else "",
                            "Partitions": len(t.get("partitions", [])),
                            "Direct Sources": len(t.get("data_sources", [])),
                        })
                    st.dataframe(table_data, use_container_width=True, hide_index=True)

                    # Show partition M code for tables with sources
                    tables_with_sources = [t for t in m["tables"] if t.get("partitions")]
                    if tables_with_sources:
                        st.markdown("**Partition Sources (M code):**")
                        for t in tables_with_sources[:10]:
                            for p in t.get("partitions", []):
                                if p.get("source"):
                                    with st.expander(f"📊 {t['name']} → {p['name']} ({p['type']})"):
                                        st.code(p["source"][:1000], language="m")

        # ─── Tables (matches inside models) ─────────────────────────────────────
        if results["tables"]:
            st.subheader(f"📊 Tables ({len(results['tables'])})")
            for item in results["tables"]:
                t = item["table"]
                with st.expander(f"📊 {t['name']} (in model: {item['model']})"):
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.markdown(f"- **Columns:** {t['columns']}")
                        st.markdown(f"- **Measures:** {t['measures']}")
                        st.markdown(f"- **Hidden:** {'Yes' if t.get('is_hidden') else 'No'}")
                    with col_b:
                        st.markdown(f"- **Model:** {item['model']}")
                        st.markdown(f"- **Partitions:** {len(t.get('partitions', []))}")
                        if t.get("data_sources"):
                            st.markdown("- **Sources:**")
                            for ds in t["data_sources"]:
                                st.markdown(f"  - `{ds}`")
                    if t.get("partitions"):
                        for p in t["partitions"]:
                            if p.get("source"):
                                st.markdown(f"**M code ({p['name']}):**")
                                st.code(p["source"][:800], language="m")

        # ─── Data Sources ───────────────────────────────────────────────────────
        if results["data_sources"]:
            st.subheader(f"🗄️ Data Sources ({len(results['data_sources'])})")
            for ds in results["data_sources"]:
                ds_type = classify_source(ds)
                icon_map = {"sql": "🗄️", "file": "📁", "web": "🌐", "sp": "☁️"}
                with st.expander(f"{icon_map.get(ds_type, '📦')} {ds}"):
                    st.markdown(f"**Type:** {ds_type.upper()}")
                    st.markdown(f"**Full path:** `{ds}`")
                    # Find consuming models
                    consumers = [m for m in lineage["models"] if ds in m.get("data_sources", [])]
                    if consumers:
                        st.markdown("**Used by models:**")
                        for m in consumers:
                            refs = m.get("referenced_by", [])
                            st.markdown(f"  - 🧊 **{m['name']}** → {len(refs)} report(s)")

        # ─── Issues ─────────────────────────────────────────────────────────────
        if results["issues"]:
            st.subheader(f"⚠️ Issues ({len(results['issues'])})")
            for i in results["issues"]:
                icon = "❌" if i["severity"] == "error" else "⚠️"
                st.markdown(f"- {icon} **{i['type']}**: {i['message']}")

# ─── Footer ─────────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown(
    "<div style='text-align:center; color:#888; font-size:0.85rem; padding:1rem 0;'>"
    "Built by <strong>Jonas Herforth</strong> · twoday"
    "</div>",
    unsafe_allow_html=True,
)
