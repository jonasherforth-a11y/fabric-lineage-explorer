"""
Enhanced PBIP Insights — DAX dependency analysis, visual field usage, comprehensive M parsing.

Ported from PBIP Documenter (JavaScript) to Python for use in the lineage tracker.
Adds:
- DAX reference extraction (measure→measure, measure→column, measure→table dependencies)
- Visual parser (which visuals use which fields from the semantic model)
- Comprehensive M expression parser (24 connector types)
- Relationship details (cardinality, cross-filter direction)
- Report page/visual inventory
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─── DAX Reference Extraction ───────────────────────────────────────────────────

@dataclass
class DAXReferences:
    """References found in a DAX expression."""
    measure_refs: list = field(default_factory=list)   # ["MeasureName", ...]
    column_refs: list = field(default_factory=list)    # [("Table", "Column"), ...]
    table_refs: list = field(default_factory=list)     # ["TableName", ...]


# DAX functions that take a table as first argument
_DAX_TABLE_FUNCTIONS = (
    "COUNTROWS|RELATEDTABLE|VALUES|ALL|ALLEXCEPT|ALLSELECTED|ALLNOBLANKROW|"
    "REMOVEFILTERS|DISTINCT|SUMMARIZE|SUMMARIZECOLUMNS|ADDCOLUMNS|SELECTCOLUMNS|"
    "FILTER|CALCULATETABLE|TOPN|GENERATE|GENERATESERIES|NATURALLEFTOUTERJOIN|"
    "NATURALINNERJOIN|CROSSJOIN|UNION|INTERSECT|EXCEPT|TREATAS|LOOKUPVALUE|"
    "RELATED|RANKX|SAMPLE|GROUPBY|DATATABLE|WINDOW|OFFSET|INDEX"
)

# Regex patterns for DAX parsing
_RE_DAX_BLOCK_COMMENT = re.compile(r'/\*[\s\S]*?\*/')
_RE_DAX_LINE_COMMENT = re.compile(r'//.*')
_RE_DAX_STRING_LITERAL = re.compile(r'"[^"]*"')
_RE_MEASURE_REF = re.compile(r'(?<!\[)(?<!\w)\[([^\]]+)\]')
_RE_COLUMN_REF = re.compile(r"(?:'([^']+)'|(\w+))\[([^\]]+)\]")
_RE_TABLE_REF = re.compile(
    rf"(?:{_DAX_TABLE_FUNCTIONS})\s*\(\s*(?:'([^']+)'|(\w+))\s*(?:[,)])",
    re.IGNORECASE
)


def _clean_dax(dax: str) -> str:
    """Remove comments and string literals from DAX for safe reference extraction."""
    cleaned = _RE_DAX_BLOCK_COMMENT.sub('', dax)
    cleaned = _RE_DAX_LINE_COMMENT.sub('', cleaned)
    cleaned = _RE_DAX_STRING_LITERAL.sub('""', cleaned)
    return cleaned


def extract_dax_references(dax_expression: str, own_table: str = "") -> DAXReferences:
    """Extract all references from a DAX expression."""
    refs = DAXReferences()
    if not dax_expression:
        return refs

    cleaned = _clean_dax(dax_expression)

    # Extract column refs: 'Table'[Column] or Table[Column]
    for match in _RE_COLUMN_REF.finditer(cleaned):
        table = match.group(1) or match.group(2)
        column = match.group(3)
        refs.column_refs.append((table, column))

    # Extract measure refs: standalone [MeasureName] (not preceded by table ref)
    # Remove column refs first to avoid double-counting
    no_cols = _RE_COLUMN_REF.sub('', cleaned)
    for match in _RE_MEASURE_REF.finditer(no_cols):
        measure_name = match.group(1)
        refs.measure_refs.append(measure_name)

    # Extract table refs from DAX functions
    for match in _RE_TABLE_REF.finditer(cleaned):
        table = match.group(1) or match.group(2)
        if table and table != own_table:
            refs.table_refs.append(table)

    # Deduplicate
    refs.measure_refs = list(dict.fromkeys(refs.measure_refs))
    refs.column_refs = list(dict.fromkeys(refs.column_refs))
    refs.table_refs = list(dict.fromkeys(refs.table_refs))

    return refs


def build_measure_dependency_graph(tables: list) -> dict:
    """Build a full measure dependency graph from parsed tables.
    
    Returns: {
        "measure_name": {
            "table": "TableName",
            "expression": "DAX...",
            "depends_on_measures": ["OtherMeasure", ...],
            "depends_on_columns": [("Table", "Column"), ...],
            "depends_on_tables": ["TableName", ...],
        }
    }
    """
    graph = {}
    for table in tables:
        for measure in table.measures:
            refs = extract_dax_references(measure.expression, table.name)
            graph[measure.name] = {
                "table": table.name,
                "expression": measure.expression,
                "depends_on_measures": refs.measure_refs,
                "depends_on_columns": refs.column_refs,
                "depends_on_tables": refs.table_refs,
            }
    return graph


def get_measure_impact(measure_name: str, dep_graph: dict, _visited: set = None) -> list:
    """Find all measures that depend on a given measure (reverse/impact analysis)."""
    if _visited is None:
        _visited = set()
    _visited.add(measure_name)

    impacted = []
    for name, info in dep_graph.items():
        if name in _visited:
            continue
        if measure_name in info["depends_on_measures"]:
            impacted.append(name)
            # Transitive
            impacted.extend(get_measure_impact(name, dep_graph, _visited))
    return list(dict.fromkeys(impacted))


def resolve_measure_chain(measure_name: str, dep_graph: dict, _visited: set = None) -> list:
    """Resolve the full transitive dependency chain for a measure."""
    if _visited is None:
        _visited = set()
    if measure_name in _visited:
        return []
    _visited.add(measure_name)

    info = dep_graph.get(measure_name)
    if not info:
        return []

    chain = []
    for dep in info["depends_on_measures"]:
        chain.append(dep)
        chain.extend(resolve_measure_chain(dep, dep_graph, _visited))
    return list(dict.fromkeys(chain))


# ─── Visual Parser ──────────────────────────────────────────────────────────────

@dataclass
class VisualFieldUsage:
    """A single field usage by a visual."""
    visual_name: str
    visual_type: str
    page_name: str
    projection: str = ""  # Values, Category, Series, Filters, Tooltips


@dataclass
class ReportVisual:
    """A visual on a report page."""
    name: str
    visual_type: str
    page_name: str
    fields: list = field(default_factory=list)  # [("type", "table", "field"), ...]


@dataclass
class ReportPage:
    """A page in a report."""
    name: str
    display_name: str = ""
    report_name: str = ""
    visuals: list = field(default_factory=list)  # [ReportVisual, ...]
    width: int = 0
    height: int = 0


def parse_visual_json(visual_data: dict, page_name: str) -> ReportVisual:
    """Parse a single visual.json and extract field references."""
    visual_type = visual_data.get("visual", {}).get("visualType", "unknown")

    # Extract visual name from title
    visual_name = ""
    vc_objects = visual_data.get("visualContainerObjects", {})
    title_arr = vc_objects.get("title", [])
    if title_arr and isinstance(title_arr, list):
        props = title_arr[0].get("properties", {})
        text = props.get("text", {})
        expr = text.get("expr", {})
        literal = expr.get("Literal", {})
        visual_name = literal.get("Value", "").strip("'\"")

    if not visual_name:
        general = visual_data.get("visual", {}).get("objects", {}).get("general", [])
        if general:
            visual_name = general[0].get("properties", {}).get("title", "")

    if not visual_name:
        visual_name = visual_type

    visual = ReportVisual(name=visual_name, visual_type=visual_type, page_name=page_name)

    # Extract fields from query state
    query = visual_data.get("visual", {}).get("query", {})
    query_state = query.get("queryState", {})

    for projection_name, projection in query_state.items():
        projections = projection.get("projections", [])
        for proj in projections:
            field_info = proj.get("field", {})
            _extract_field_from_pbir(field_info, visual, projection_name)

    # Extract from sort definitions
    sort_def = query.get("sortDefinition", {})
    for sort_item in sort_def.get("sort", []):
        field_info = sort_item.get("field", {})
        _extract_field_from_pbir(field_info, visual, "Sort")

    # Extract from filter config
    filter_config = visual_data.get("filterConfig", {})
    for f in filter_config.get("filters", []):
        field_info = f.get("filter", {}).get("Where", [{}])[0].get("Condition", {}).get("Left", {}).get("Column", {}) if "filter" in f else {}
        if field_info:
            entity = field_info.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
            prop = field_info.get("Property", "")
            if entity and prop:
                visual.fields.append(("Column", entity, prop))

    return visual


def _extract_field_from_pbir(field_info: dict, visual: ReportVisual, projection: str):
    """Extract a field reference from PBIR field JSON structure."""
    # Column reference
    if "Column" in field_info:
        col = field_info["Column"]
        entity = col.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
        prop = col.get("Property", "")
        if entity and prop:
            visual.fields.append(("Column", entity, prop))

    # Measure reference
    elif "Measure" in field_info:
        meas = field_info["Measure"]
        entity = meas.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
        prop = meas.get("Property", "")
        if entity and prop:
            visual.fields.append(("Measure", entity, prop))

    # Hierarchy reference
    elif "Hierarchy" in field_info:
        hier = field_info["Hierarchy"]
        entity = hier.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
        prop = hier.get("Hierarchy", "")
        if entity and prop:
            visual.fields.append(("Hierarchy", entity, prop))


def parse_report_pages(report_path: Path) -> list[ReportPage]:
    """Parse all pages and visuals from a .Report folder."""
    pages = []
    definition_dir = report_path / "definition"
    pages_dir = definition_dir / "pages"

    # Handle nested .Report folder structure
    if not pages_dir.exists():
        for child in report_path.iterdir():
            if child.is_dir() and child.name.endswith(".Report"):
                pages_dir = child / "definition" / "pages"
                break

    if not pages_dir.exists():
        return pages

    for page_dir in sorted(pages_dir.iterdir()):
        if not page_dir.is_dir():
            continue

        page = ReportPage(name=page_dir.name)

        # Read page.json for display name and dimensions
        page_json = page_dir / "page.json"
        if page_json.exists():
            try:
                data = json.loads(page_json.read_text(encoding="utf-8-sig"))
                page.display_name = data.get("displayName", page_dir.name)
                page.width = data.get("width", 0)
                page.height = data.get("height", 0)
            except (json.JSONDecodeError, UnicodeDecodeError):
                page.display_name = page_dir.name

        # Parse visuals
        visuals_dir = page_dir / "visuals"
        if visuals_dir.exists():
            for visual_dir in sorted(visuals_dir.iterdir()):
                if not visual_dir.is_dir():
                    continue
                visual_json = visual_dir / "visual.json"
                if visual_json.exists():
                    try:
                        vdata = json.loads(visual_json.read_text(encoding="utf-8-sig"))
                        visual = parse_visual_json(vdata, page.display_name or page.name)
                        page.visuals.append(visual)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass

        pages.append(page)

    return pages


def build_field_usage_map(pages: list[ReportPage]) -> dict:
    """Build a field usage map: 'type|table|field' → [VisualFieldUsage, ...]"""
    usage_map = {}
    for page in pages:
        for visual in page.visuals:
            for field_type, table, field_name in visual.fields:
                key = f"{field_type}|{table}|{field_name}"
                if key not in usage_map:
                    usage_map[key] = []
                usage_map[key].append(VisualFieldUsage(
                    visual_name=visual.name,
                    visual_type=visual.visual_type,
                    page_name=page.display_name or page.name,
                ))
    return usage_map


# ─── Comprehensive M Expression Parser ──────────────────────────────────────────

@dataclass
class DataSourceInfo:
    """A data source extracted from M code."""
    source_type: str  # sql_server, sharepoint, web, excel, etc.
    server: str = ""
    database: str = ""
    url: str = ""
    path: str = ""
    table_name: str = ""      # Which TMDL table it belongs to
    partition_name: str = ""   # Which partition
    parameterized: bool = False
    gateway_required: str = "unknown"  # on-prem, cloud, unknown


# M connector patterns: (type, regex, field_mapping)
_M_CONNECTORS = [
    ("sql_server", re.compile(r'Sql\.(?:Database|Databases)\s*\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?', re.IGNORECASE), ("server", "database")),
    ("analysis_services", re.compile(r'AnalysisServices\.Database\s*\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?', re.IGNORECASE), ("server", "database")),
    ("odata", re.compile(r'OData\.Feed\s*\(\s*"([^"]*)"', re.IGNORECASE), ("url",)),
    ("web", re.compile(r'Web\.(?:Contents|Page)\s*\(\s*"([^"]*)"', re.IGNORECASE), ("url",)),
    ("sharepoint_tables", re.compile(r'SharePoint\.Tables\s*\(\s*"([^"]*)"', re.IGNORECASE), ("url",)),
    ("sharepoint_files", re.compile(r'SharePoint\.Files\s*\(\s*"([^"]*)"', re.IGNORECASE), ("url",)),
    ("excel", re.compile(r'Excel\.Workbook\s*\(\s*File\.Contents\s*\(\s*"([^"]*)"', re.IGNORECASE), ("path",)),
    ("csv", re.compile(r'Csv\.Document\s*\(\s*File\.Contents\s*\(\s*"([^"]*)"', re.IGNORECASE), ("path",)),
    ("azure_blob", re.compile(r'AzureStorage\.Blobs\s*\(\s*"([^"]*)"', re.IGNORECASE), ("url",)),
    ("dataverse", re.compile(r'(?:CommonDataService|Dataverse)\.Contents\s*\(\s*"([^"]*)"', re.IGNORECASE), ("url",)),
    ("snowflake", re.compile(r'Snowflake\.Databases\s*\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?', re.IGNORECASE), ("server", "database")),
    ("oracle", re.compile(r'Oracle\.Database\s*\(\s*"([^"]*)"', re.IGNORECASE), ("server",)),
    ("bigquery", re.compile(r'GoogleBigQuery\.Database\s*\(\s*"?([^"]*)"?', re.IGNORECASE), ("server",)),
    ("postgresql", re.compile(r'PostgreSQL\.Database\s*\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?', re.IGNORECASE), ("server", "database")),
    ("mysql", re.compile(r'MySQL\.Database\s*\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?', re.IGNORECASE), ("server", "database")),
    ("teradata", re.compile(r'Teradata\.Database\s*\(\s*"([^"]*)"', re.IGNORECASE), ("server",)),
    ("sap_hana", re.compile(r'SapHana\.Database\s*\(\s*"([^"]*)"', re.IGNORECASE), ("server",)),
    ("odbc", re.compile(r'Odbc\.(?:DataSource|Query)\s*\(\s*"([^"]*)"', re.IGNORECASE), ("server",)),
    ("pbi_dataflow", re.compile(r'PowerBI\.Dataflows\s*\(', re.IGNORECASE), ()),
    ("azure_data_explorer", re.compile(r'(?:AzureDataExplorer|Kusto)\.(?:Contents|Database)\s*\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?', re.IGNORECASE), ("server", "database")),
    ("fabric_lakehouse", re.compile(r'Lakehouse\.Contents\s*\(', re.IGNORECASE), ()),
    ("fabric_warehouse", re.compile(r'Fabric\.Warehouse\s*\(\s*"([^"]*)"', re.IGNORECASE), ("server",)),
    ("databricks", re.compile(r'Databricks\.Catalogs\s*\(\s*"([^"]*)"', re.IGNORECASE), ("server",)),
]

_ON_PREM_TYPES = {"sql_server", "oracle", "teradata", "sap_hana", "odbc", "analysis_services"}
_CLOUD_RE = re.compile(
    r'\.database\.windows\.net|\.sql\.azuresynapse\.net|\.datawarehouse\.fabric\.microsoft\.com|'
    r'\.pbidedicated\.windows\.net|\.asazure\.windows\.net',
    re.IGNORECASE
)
_ALWAYS_CLOUD = {"azure_blob", "dataverse", "snowflake", "bigquery", "pbi_dataflow", "odata",
                 "sharepoint_tables", "sharepoint_files", "fabric_lakehouse", "fabric_warehouse",
                 "azure_data_explorer", "databricks", "web"}


def extract_m_data_sources(m_expression: str, table_name: str = "", partition_name: str = "") -> list[DataSourceInfo]:
    """Extract all data sources from an M expression using comprehensive connector patterns."""
    sources = []

    for source_type, pattern, fields in _M_CONNECTORS:
        for match in pattern.finditer(m_expression):
            ds = DataSourceInfo(
                source_type=source_type,
                table_name=table_name,
                partition_name=partition_name,
            )

            # Map captured groups to fields
            for i, field_name in enumerate(fields):
                value = match.group(i + 1) if i + 1 <= len(match.groups()) else ""
                if value:
                    setattr(ds, field_name, value)

            # Check if parameterized (contains #"...")
            if '#"' in m_expression[max(0, match.start()-20):match.end()+50]:
                ds.parameterized = True

            # Classify gateway requirement
            if source_type in _ALWAYS_CLOUD:
                ds.gateway_required = "cloud"
            elif source_type in _ON_PREM_TYPES:
                server_val = ds.server or ds.url or ""
                if _CLOUD_RE.search(server_val):
                    ds.gateway_required = "cloud"
                else:
                    ds.gateway_required = "on-prem"
            else:
                ds.gateway_required = "cloud"

            sources.append(ds)

    return sources


def extract_all_data_sources(tables: list, expressions: list = None) -> list[DataSourceInfo]:
    """Extract all data sources from all table partitions, with parameter resolution."""
    all_sources = []

    # Build parameter map from expressions
    param_map = {}
    if expressions:
        for expr in expressions:
            if hasattr(expr, 'expression') and expr.expression:
                # Check if it's a parameter query (contains IsParameterQuery)
                if "IsParameterQuery" in expr.expression or "IsParameterQuery" in str(getattr(expr, 'annotations', '')):
                    # Extract literal value
                    val_match = re.search(r'"([^"]+)"\s*meta', expr.expression)
                    if val_match:
                        param_map[expr.name] = val_match.group(1)

    for table in tables:
        for partition in table.partitions:
            if partition.source_expression:
                sources = extract_m_data_sources(
                    partition.source_expression,
                    table_name=table.name,
                    partition_name=partition.name,
                )
                # Resolve parameters
                for src in sources:
                    for attr in ("server", "database", "url", "path"):
                        val = getattr(src, attr, "")
                        if val and val in param_map:
                            setattr(src, attr, param_map[val])
                            src.parameterized = True
                all_sources.extend(sources)

    return _deduplicate_sources(all_sources)


def _deduplicate_sources(sources: list[DataSourceInfo]) -> list[DataSourceInfo]:
    """Deduplicate data sources by type+server+database."""
    seen = set()
    unique = []
    for s in sources:
        key = f"{s.source_type}|{s.server}|{s.database}|{s.url}|{s.path}"
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


# ─── Enhanced Relationship Parsing ───────────────────────────────────────────────

@dataclass
class EnhancedRelationship:
    """Full relationship details matching PBIP Documenter output."""
    id: str = ""
    from_table: str = ""
    from_column: str = ""
    to_table: str = ""
    to_column: str = ""
    from_cardinality: str = "many"   # one, many
    to_cardinality: str = "one"      # one, many
    cross_filtering: str = "oneDirection"  # oneDirection, bothDirections
    is_active: bool = True


def parse_relationships_enhanced(file_path: Path) -> list[EnhancedRelationship]:
    """Parse relationships.tmdl with full cardinality and cross-filter info."""
    if not file_path.exists():
        return []

    content = file_path.read_text(encoding="utf-8-sig")
    relationships = []
    current = None

    for line in content.split("\n"):
        stripped = line.strip()

        if stripped.startswith("relationship "):
            if current:
                relationships.append(current)
            current = EnhancedRelationship(id=stripped[13:].strip())

        elif current:
            if stripped.startswith("fromColumn:"):
                ref = stripped.split(":", 1)[1].strip()
                parts = _parse_col_ref(ref)
                current.from_table = parts[0]
                current.from_column = parts[1]
            elif stripped.startswith("toColumn:"):
                ref = stripped.split(":", 1)[1].strip()
                parts = _parse_col_ref(ref)
                current.to_table = parts[0]
                current.to_column = parts[1]
            elif stripped.startswith("fromCardinality:"):
                current.from_cardinality = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("toCardinality:"):
                current.to_cardinality = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("crossFilteringBehavior:"):
                current.cross_filtering = stripped.split(":", 1)[1].strip()
            elif stripped == "isActive: false":
                current.is_active = False

    if current:
        relationships.append(current)

    return relationships


def _parse_col_ref(ref: str) -> tuple:
    """Parse 'Table'.Column reference."""
    match = re.match(r"'([^']+)'\.(.+)", ref)
    if match:
        return match.group(1), match.group(2)
    parts = ref.split(".", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (ref, "")


# ─── Roles (RLS) Parser ─────────────────────────────────────────────────────────

@dataclass
class RolePermission:
    table: str
    filter_expression: str = ""


@dataclass
class SecurityRole:
    name: str
    model_permission: str = "read"
    table_permissions: list = field(default_factory=list)


def parse_roles(roles_dir: Path) -> list[SecurityRole]:
    """Parse all role .tmdl files from the roles/ directory."""
    roles = []
    if not roles_dir.exists():
        return roles

    for role_file in sorted(roles_dir.glob("*.tmdl")):
        content = role_file.read_text(encoding="utf-8-sig")
        role = SecurityRole(name=role_file.stem)
        current_table = None
        in_expression = False
        expr_lines = []

        for line in content.split("\n"):
            stripped = line.strip()

            if stripped.startswith("role "):
                role.name = stripped[5:].strip().strip("'")
            elif stripped.startswith("modelPermission:"):
                role.model_permission = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("tablePermission "):
                if current_table and expr_lines:
                    current_table.filter_expression = "\n".join(expr_lines).strip()
                    expr_lines = []
                table_name = stripped[16:].strip().strip("'")
                current_table = RolePermission(table=table_name)
                role.table_permissions.append(current_table)
                in_expression = False
            elif stripped.startswith("filterExpression") or in_expression:
                if stripped.startswith("filterExpression"):
                    in_expression = True
                    # May have = ``` or = value on same line
                    if "```" in stripped:
                        pass
                    elif "=" in stripped:
                        expr_lines.append(stripped.split("=", 1)[1].strip())
                elif stripped == "```":
                    in_expression = False
                else:
                    expr_lines.append(line.rstrip())

        if current_table and expr_lines:
            current_table.filter_expression = "\n".join(expr_lines).strip()

        roles.append(role)

    return roles


# ─── Expressions (Named M Queries) ──────────────────────────────────────────────

@dataclass
class NamedExpression:
    name: str
    kind: str = "m"
    expression: str = ""
    is_parameter: bool = False


def parse_expressions(file_path: Path) -> list[NamedExpression]:
    """Parse expressions.tmdl for named M queries and parameters."""
    if not file_path.exists():
        return []

    content = file_path.read_text(encoding="utf-8-sig")
    expressions = []
    current = None
    in_expression = False
    expr_lines = []

    for line in content.split("\n"):
        stripped = line.strip()

        if stripped.startswith("expression "):
            if current:
                current.expression = "\n".join(expr_lines).strip()
                if "IsParameterQuery" in current.expression:
                    current.is_parameter = True
                expressions.append(current)
                expr_lines = []

            name = stripped[11:].strip().strip("'")
            # Handle "expression Name = m" format
            if "=" in name:
                parts = name.split("=", 1)
                name = parts[0].strip().strip("'")
            current = NamedExpression(name=name)
            in_expression = False

        elif stripped.startswith("kind:") and current:
            current.kind = stripped.split(":", 1)[1].strip()

        elif (stripped.startswith("expression") and "=" in stripped) or in_expression:
            if not in_expression:
                in_expression = True
                if "```" not in stripped and "=" in stripped:
                    expr_lines.append(stripped.split("=", 1)[1].strip())
            elif stripped == "```":
                in_expression = False
            else:
                expr_lines.append(line.rstrip())

    if current:
        current.expression = "\n".join(expr_lines).strip()
        if "IsParameterQuery" in current.expression:
            current.is_parameter = True
        expressions.append(current)

    return expressions


# ─── Full Enhanced Scan ──────────────────────────────────────────────────────────

def scan_semantic_model_enhanced(model_path: Path) -> dict:
    """Full enhanced scan of a semantic model with all PBIP Documenter capabilities.
    
    Returns dict with:
    - tables, relationships, roles, expressions, data_sources
    - measure_dependencies (DAX graph)
    - database_info, model_info
    """
    from tmdl_parser import scan_semantic_model, TmdlTable

    # Get base scan
    base = scan_semantic_model(model_path)

    # Find definition dir
    definition_dir = model_path / "definition"
    if not definition_dir.exists():
        for child in model_path.iterdir():
            if child.is_dir() and "SemanticModel" in child.name:
                definition_dir = child / "definition"
                break

    result = {
        "name": base.name,
        "path": str(base.path),
        "tables": base.tables,
        "relationships_basic": base.relationships,
        "data_sources_basic": list(base.data_sources),
    }

    # Enhanced relationships
    rel_file = definition_dir / "relationships.tmdl"
    result["relationships"] = parse_relationships_enhanced(rel_file)

    # Roles
    roles_dir = definition_dir / "roles"
    result["roles"] = parse_roles(roles_dir)

    # Expressions
    expr_file = definition_dir / "expressions.tmdl"
    result["expressions"] = parse_expressions(expr_file)

    # Enhanced data sources (24 connector types)
    result["data_sources"] = extract_all_data_sources(base.tables, result["expressions"])

    # Measure dependency graph
    result["measure_dependencies"] = build_measure_dependency_graph(base.tables)

    # Database info
    db_file = definition_dir / "database.tmdl"
    result["database_info"] = _parse_database_tmdl(db_file)

    # Model info
    model_file = definition_dir / "model.tmdl"
    result["model_info"] = _parse_model_tmdl(model_file)

    return result


def scan_report_enhanced(report_path: Path) -> dict:
    """Full enhanced scan of a report with visual field usage."""
    from tmdl_parser import scan_report

    base = scan_report(report_path)

    result = {
        "name": base.name,
        "path": str(base.path),
        "semantic_model_id": base.semantic_model_id,
        "semantic_model_name": base.semantic_model_name,
        "workspace_name": base.workspace_name,
    }

    # Parse pages and visuals
    pages = parse_report_pages(report_path)
    result["pages"] = pages
    result["page_count"] = len(pages)
    result["visual_count"] = sum(len(p.visuals) for p in pages)

    # Build field usage map
    result["field_usage_map"] = build_field_usage_map(pages)

    return result


def _parse_database_tmdl(file_path: Path) -> dict:
    """Parse database.tmdl for name and compatibility level."""
    if not file_path.exists():
        return {}
    content = file_path.read_text(encoding="utf-8-sig")
    info = {}
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("name:"):
            info["name"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("compatibilityLevel:"):
            info["compatibilityLevel"] = stripped.split(":", 1)[1].strip()
    return info


def _parse_model_tmdl(file_path: Path) -> dict:
    """Parse model.tmdl for culture and other model-level properties."""
    if not file_path.exists():
        return {}
    content = file_path.read_text(encoding="utf-8-sig")
    info = {}
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("culture:"):
            info["culture"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("defaultPowerBIDataSourceVersion:"):
            info["defaultPowerBIDataSourceVersion"] = stripped.split(":", 1)[1].strip()
    return info
