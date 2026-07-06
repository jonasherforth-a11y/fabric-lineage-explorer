"""
TMDL Parser — Extracts lineage from PBIP/TMDL files in Git repos.

Parses:
- definition.pbir → report-to-model binding
- tables/*.tmdl → table definitions, columns, partition source expressions (M code)
- relationships.tmdl → relationship map
- measures in table files → measure definitions

Works entirely offline — no API calls needed. Ideal for CI/CD pipelines.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TmdlColumn:
    name: str
    data_type: str = ""
    source_column: str = ""
    is_hidden: bool = False
    lineage_tag: str = ""


@dataclass
class TmdlMeasure:
    name: str
    expression: str = ""
    table: str = ""
    lineage_tag: str = ""


@dataclass
class TmdlPartition:
    name: str
    source_type: str = "m"  # m, entity, calculated
    source_expression: str = ""
    query_group: str = ""


@dataclass
class TmdlTable:
    name: str
    columns: list = field(default_factory=list)
    measures: list = field(default_factory=list)
    partitions: list = field(default_factory=list)
    is_hidden: bool = False
    lineage_tag: str = ""
    data_sources: list = field(default_factory=list)  # Extracted from M expressions


@dataclass
class TmdlRelationship:
    id: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True


@dataclass
class SemanticModelDef:
    name: str
    path: Path
    tables: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    data_sources: set = field(default_factory=set)


@dataclass
class ReportDef:
    name: str
    path: Path
    semantic_model_id: str = ""
    semantic_model_name: str = ""
    workspace_name: str = ""
    connection_string: str = ""
    pages: list = field(default_factory=list)


def parse_tmdl_table(file_path: Path) -> TmdlTable:
    """Parse a single .tmdl table file."""
    content = file_path.read_text(encoding="utf-8-sig")
    lines = content.split("\n")

    table_name = ""
    columns = []
    measures = []
    partitions = []
    is_hidden = False
    lineage_tag = ""
    data_sources = []

    # Parse table header
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("table "):
            table_name = stripped[6:].strip().strip("'")
            break

    # State machine for parsing
    current_section = None  # "column", "measure", "partition"
    current_item = {}
    partition_source_lines = []
    in_source_block = False

    for line in lines:
        stripped = line.strip()

        # Table-level properties
        if stripped == "isHidden" and current_section is None:
            is_hidden = True
        elif stripped.startswith("lineageTag:") and current_section is None:
            lineage_tag = stripped.split(":", 1)[1].strip()

        # New column
        elif stripped.startswith("column "):
            _flush_item(current_section, current_item, columns, measures, partitions, partition_source_lines)
            current_section = "column"
            col_name = stripped[7:].strip().strip("'")
            current_item = {"name": col_name, "data_type": "", "source_column": "", "is_hidden": False, "lineage_tag": ""}
            partition_source_lines = []
            in_source_block = False

        # New measure
        elif stripped.startswith("measure "):
            _flush_item(current_section, current_item, columns, measures, partitions, partition_source_lines)
            current_section = "measure"
            measure_name = stripped[8:].strip().strip("'")
            # Handle = on same line
            if "=" in measure_name:
                parts = measure_name.split("=", 1)
                measure_name = parts[0].strip().strip("'")
                current_item = {"name": measure_name, "expression": parts[1].strip(), "lineage_tag": ""}
            else:
                current_item = {"name": measure_name, "expression": "", "lineage_tag": ""}
            partition_source_lines = []
            in_source_block = False

        # New partition
        elif stripped.startswith("partition "):
            _flush_item(current_section, current_item, columns, measures, partitions, partition_source_lines)
            current_section = "partition"
            # partition 'Name' = m
            match = re.match(r"partition\s+'?([^'=]+)'?\s*=\s*(\w+)", stripped)
            if match:
                current_item = {"name": match.group(1).strip(), "source_type": match.group(2), "query_group": ""}
            else:
                current_item = {"name": "default", "source_type": "m", "query_group": ""}
            partition_source_lines = []
            in_source_block = False

        # Properties within sections
        elif current_section == "column":
            if stripped.startswith("dataType:"):
                current_item["data_type"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("sourceColumn:"):
                current_item["source_column"] = stripped.split(":", 1)[1].strip()
            elif stripped == "isHidden":
                current_item["is_hidden"] = True
            elif stripped.startswith("lineageTag:"):
                current_item["lineage_tag"] = stripped.split(":", 1)[1].strip()

        elif current_section == "measure":
            if stripped.startswith("lineageTag:"):
                current_item["lineage_tag"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("expression") or (current_item.get("expression") and not stripped.startswith("lineageTag") and not stripped.startswith("formatString") and not stripped.startswith("displayFolder")):
                # Multi-line expression handling
                if stripped.startswith("expression"):
                    expr_part = stripped.split("=", 1)[1].strip() if "=" in stripped else ""
                    current_item["expression"] = expr_part
                elif not any(stripped.startswith(kw) for kw in ["formatString", "displayFolder", "description", "annotation"]):
                    current_item["expression"] += "\n" + stripped

        elif current_section == "partition":
            if stripped.startswith("queryGroup:"):
                current_item["query_group"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("source") or in_source_block:
                if stripped.startswith("source"):
                    in_source_block = True
                    # source = ``` starts a block
                    if "```" in stripped:
                        pass  # M code follows
                    else:
                        source_part = stripped.split("=", 1)[1].strip() if "=" in stripped else ""
                        partition_source_lines.append(source_part)
                elif stripped == "```":
                    in_source_block = False
                else:
                    partition_source_lines.append(line.rstrip())

    # Flush last item
    _flush_item(current_section, current_item, columns, measures, partitions, partition_source_lines)

    # Extract data sources from partition M expressions
    for p in partitions:
        sources = extract_data_sources_from_m(p.source_expression)
        data_sources.extend(sources)

    # Also check the comment header for M code (some TMDL files have it)
    header_m = extract_m_from_header(content)
    if header_m:
        data_sources.extend(extract_data_sources_from_m(header_m))

    return TmdlTable(
        name=table_name,
        columns=columns,
        measures=measures,
        partitions=partitions,
        is_hidden=is_hidden,
        lineage_tag=lineage_tag,
        data_sources=list(set(data_sources))
    )


def _flush_item(section, item, columns, measures, partitions, partition_source_lines):
    """Flush current item to its list."""
    if not item:
        return
    if section == "column":
        columns.append(TmdlColumn(**item))
    elif section == "measure":
        measures.append(TmdlMeasure(
            name=item["name"],
            expression=item.get("expression", "").strip(),
            lineage_tag=item.get("lineage_tag", "")
        ))
    elif section == "partition":
        source_expr = "\n".join(partition_source_lines).strip()
        partitions.append(TmdlPartition(
            name=item["name"],
            source_type=item.get("source_type", "m"),
            source_expression=source_expr,
            query_group=item.get("query_group", "")
        ))


def extract_m_from_header(content: str) -> str:
    """Extract M code from /// comment header in TMDL files."""
    lines = content.split("\n")
    m_lines = []
    for line in lines:
        if line.strip().startswith("///"):
            m_lines.append(line.strip()[3:].strip())
        elif m_lines:
            break
    return "\n".join(m_lines)


def extract_data_sources_from_m(m_code: str) -> list[str]:
    """Extract data source references from M (Power Query) code."""
    sources = []

    # SQL Server: Sql.Database("server", "database")
    sql_matches = re.findall(r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)', m_code)
    for server, db in sql_matches:
        sources.append(f"SQL://{server}/{db}")

    # Excel: Excel.Workbook(File.Contents("path"))
    excel_matches = re.findall(r'File\.Contents\(\s*"([^"]+)"\s*\)', m_code)
    for path in excel_matches:
        sources.append(f"File://{path}")

    # SharePoint: SharePoint.Tables("url") or SharePoint.Contents("url")
    sp_matches = re.findall(r'SharePoint\.\w+\(\s*"([^"]+)"\s*\)', m_code)
    for url in sp_matches:
        sources.append(f"SharePoint://{url}")

    # Web: Web.Contents("url")
    web_matches = re.findall(r'Web\.Contents\(\s*"([^"]+)"\s*\)', m_code)
    for url in web_matches:
        sources.append(f"Web://{url}")

    # Lakehouse: Lakehouse.Contents or similar
    lake_matches = re.findall(r'Lakehouse\.\w+\(\s*"([^"]+)"\s*\)', m_code)
    for ref in lake_matches:
        sources.append(f"Lakehouse://{ref}")

    return sources


def parse_relationships_tmdl(file_path: Path) -> list[TmdlRelationship]:
    """Parse relationships.tmdl file."""
    if not file_path.exists():
        return []

    content = file_path.read_text(encoding="utf-8-sig")
    relationships = []

    current_id = ""
    from_col = ""
    to_col = ""
    is_active = True

    for line in content.split("\n"):
        stripped = line.strip()

        if stripped.startswith("relationship "):
            # Flush previous
            if current_id and from_col and to_col:
                from_parts = _parse_column_ref(from_col)
                to_parts = _parse_column_ref(to_col)
                relationships.append(TmdlRelationship(
                    id=current_id,
                    from_table=from_parts[0],
                    from_column=from_parts[1],
                    to_table=to_parts[0],
                    to_column=to_parts[1],
                    is_active=is_active
                ))
            current_id = stripped[13:].strip()
            from_col = ""
            to_col = ""
            is_active = True

        elif stripped.startswith("fromColumn:"):
            from_col = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("toColumn:"):
            to_col = stripped.split(":", 1)[1].strip()
        elif stripped == "isActive: false":
            is_active = False

    # Flush last
    if current_id and from_col and to_col:
        from_parts = _parse_column_ref(from_col)
        to_parts = _parse_column_ref(to_col)
        relationships.append(TmdlRelationship(
            id=current_id,
            from_table=from_parts[0],
            from_column=from_parts[1],
            to_table=to_parts[0],
            to_column=to_parts[1],
            is_active=is_active
        ))

    return relationships


def _parse_column_ref(ref: str) -> tuple[str, str]:
    """Parse 'Table'.Column or Table.Column reference."""
    # Match 'Table Name'.ColumnName
    match = re.match(r"'([^']+)'\.(.+)", ref)
    if match:
        return match.group(1), match.group(2)
    # Match Table.Column (no quotes)
    parts = ref.split(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return ref, ""


def parse_definition_pbir(file_path: Path) -> dict:
    """Parse definition.pbir to extract semantic model binding."""
    content = json.loads(file_path.read_text(encoding="utf-8-sig"))
    result = {
        "semantic_model_id": "",
        "semantic_model_name": "",
        "workspace_name": "",
        "connection_string": "",
    }

    ds_ref = content.get("datasetReference", {})
    by_conn = ds_ref.get("byConnection", {})
    conn_str = by_conn.get("connectionString", "")
    result["connection_string"] = conn_str

    # Extract semanticmodelid from connection string
    id_match = re.search(r"semanticmodelid=([a-f0-9-]+)", conn_str, re.IGNORECASE)
    if id_match:
        result["semantic_model_id"] = id_match.group(1)

    # Extract catalog (model name)
    catalog_match = re.search(r'initial catalog="([^"]+)"', conn_str, re.IGNORECASE)
    if catalog_match:
        result["semantic_model_name"] = catalog_match.group(1)

    # Extract workspace from data source
    ws_match = re.search(r'myorg/([^"]+)"', conn_str)
    if ws_match:
        result["workspace_name"] = ws_match.group(1)

    return result


def scan_semantic_model(model_path: Path) -> SemanticModelDef:
    """Scan a PBIP semantic model folder and extract full metadata."""
    # Find the definition folder (contains tables/, relationships.tmdl, etc.)
    definition_dir = model_path / "definition"
    if not definition_dir.exists():
        # Try looking for .SemanticModel subfolder
        for child in model_path.iterdir():
            if child.is_dir() and "SemanticModel" in child.name:
                definition_dir = child / "definition"
                break

    model_name = model_path.name
    if ".SemanticModel" in model_name:
        model_name = model_name.replace(".SemanticModel", "")

    model = SemanticModelDef(name=model_name, path=model_path)

    if not definition_dir.exists():
        return model

    # Parse tables
    tables_dir = definition_dir / "tables"
    if tables_dir.exists():
        for tmdl_file in sorted(tables_dir.glob("*.tmdl")):
            table = parse_tmdl_table(tmdl_file)
            if table.name:
                model.tables.append(table)
                for ds in table.data_sources:
                    model.data_sources.add(ds)

    # Parse relationships
    rel_file = definition_dir / "relationships.tmdl"
    model.relationships = parse_relationships_tmdl(rel_file)

    return model


def scan_report(report_path: Path) -> ReportDef:
    """Scan a PBIP report folder and extract binding info."""
    report_name = report_path.name
    if ".Report" in report_name:
        report_name = report_name.replace(".Report", "")

    report = ReportDef(name=report_name, path=report_path)

    # Parse definition.pbir
    pbir_file = report_path / "definition.pbir"
    if pbir_file.exists():
        binding = parse_definition_pbir(pbir_file)
        report.semantic_model_id = binding["semantic_model_id"]
        report.semantic_model_name = binding["semantic_model_name"]
        report.workspace_name = binding["workspace_name"]
        report.connection_string = binding["connection_string"]

    # Extract pages from definition folder
    definition_dir = report_path / "definition"
    if definition_dir.exists():
        pages_dir = definition_dir / "pages"
        if pages_dir.exists():
            for page_dir in sorted(pages_dir.iterdir()):
                if page_dir.is_dir():
                    report.pages.append(page_dir.name)

    return report


def scan_solution_folder(solution_path: Path) -> dict:
    """Scan an entire solution folder for all models and reports."""
    models = []
    reports = []

    # Find all .SemanticModel folders
    for sm_dir in solution_path.rglob("*.SemanticModel"):
        if sm_dir.is_dir():
            model = scan_semantic_model(sm_dir)
            models.append(model)

    # Find all .Report folders
    for rpt_dir in solution_path.rglob("*.Report"):
        if rpt_dir.is_dir():
            report = scan_report(rpt_dir)
            reports.append(report)

    return {"models": models, "reports": reports}
