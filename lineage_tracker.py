"""
Fabric Object Lineage Tracker — POC
Traces Power BI objects from Report → Semantic Model → Tables → Data Sources.

Uses Microsoft Fabric REST API + DAX queries via XMLA endpoint.
Requires: az login (Azure CLI authenticated session)
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field

import msal
import requests
from rich.console import Console
from rich.tree import Tree
from rich.table import Table
from rich.panel import Panel

console = Console(force_terminal=True, color_system="truecolor")

FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
PBI_API_BASE = "https://api.powerbi.com/v1.0/myorg"

# Microsoft first-party "Azure PowerShell" public client — works for delegated user auth
CLIENT_ID = "1950a258-227b-4e31-a9cf-717495945fc2"
AUTHORITY = "https://login.microsoftonline.com/common"

_msal_app = None
_token_cache = {}


def _get_msal_app():
    global _msal_app
    if _msal_app is None:
        _msal_app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY)
    return _msal_app


def get_access_token(scope: str) -> str:
    """Get access token via MSAL interactive browser login (cached per scope)."""
    if scope in _token_cache:
        # Try silent first
        app = _get_msal_app()
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent([scope], account=accounts[0])
            if result and "access_token" in result:
                return result["access_token"]

    app = _get_msal_app()
    # Try silent with cached accounts
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent([scope], account=accounts[0])
        if result and "access_token" in result:
            _token_cache[scope] = True
            return result["access_token"]

    # Interactive browser login
    console.print(f"[yellow]Opening browser for authentication (scope: {scope})...[/yellow]")
    result = app.acquire_token_interactive(scopes=[scope])
    if "access_token" in result:
        _token_cache[scope] = True
        return result["access_token"]
    else:
        error = result.get("error_description", result.get("error", "Unknown error"))
        raise RuntimeError(f"Authentication failed: {error}")


def get_fabric_token() -> str:
    """Get Fabric API token."""
    return get_access_token("https://api.fabric.microsoft.com/.default")


def get_pbi_token() -> str:
    """Get Power BI API token."""
    return get_access_token("https://analysis.windows.net/powerbi/api/.default")


@dataclass
class DataSource:
    source_type: str
    name: str
    connection_string: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class Partition:
    name: str
    source_type: str
    source_expression: str = ""
    storage_mode: str = "Import"


@dataclass
class TableInfo:
    name: str
    columns: list = field(default_factory=list)
    partitions: list = field(default_factory=list)
    storage_mode: str = "Import"
    is_hidden: bool = False


@dataclass
class MeasureInfo:
    name: str
    expression: str
    table: str


@dataclass
class RelationshipInfo:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: str
    cross_filter: str = "Single"


@dataclass
class SemanticModelInfo:
    name: str
    model_id: str
    tables: list = field(default_factory=list)
    measures: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    data_sources: list = field(default_factory=list)


@dataclass
class ReportInfo:
    name: str
    report_id: str
    workspace_id: str
    workspace_name: str
    pages: list = field(default_factory=list)
    semantic_model: SemanticModelInfo = None


class FabricLineageTracker:
    """Tracks lineage from Power BI Report → Semantic Model → Data Sources."""

    def __init__(self):
        self.fabric_token = get_fabric_token()
        self.pbi_token = get_pbi_token()
        self.fabric_headers = {"Authorization": f"Bearer {self.fabric_token}"}
        self.pbi_headers = {"Authorization": f"Bearer {self.pbi_token}"}
        self._model_cache: dict[str, SemanticModelInfo] = {}  # Cache by dataset_id

    def list_workspaces(self) -> list[dict]:
        """List all accessible workspaces."""
        resp = requests.get(
            f"{FABRIC_API_BASE}/workspaces",
            headers=self.fabric_headers,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("value", [])

    def find_workspace(self, name: str) -> dict | None:
        """Find workspace by name."""
        workspaces = self.list_workspaces()
        for ws in workspaces:
            if ws["displayName"].lower() == name.lower():
                return ws
        # Partial match fallback
        for ws in workspaces:
            if name.lower() in ws["displayName"].lower():
                return ws
        return None

    def list_reports(self, workspace_id: str) -> list[dict]:
        """List reports in a workspace."""
        resp = requests.get(
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/reports",
            headers=self.fabric_headers,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("value", [])

    def list_datasets(self, workspace_id: str) -> list[dict]:
        """List semantic models (datasets) in a workspace."""
        resp = requests.get(
            f"{PBI_API_BASE}/groups/{workspace_id}/datasets",
            headers=self.pbi_headers,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("value", [])

    def get_report_details(self, workspace_id: str, report_id: str) -> dict:
        """Get report details including dataset binding."""
        # Use Power BI API for richer report metadata
        resp = requests.get(
            f"{PBI_API_BASE}/groups/{workspace_id}/reports/{report_id}",
            headers=self.pbi_headers,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def get_report_pages(self, workspace_id: str, report_id: str) -> list[dict]:
        """Get report pages."""
        resp = requests.get(
            f"{PBI_API_BASE}/groups/{workspace_id}/reports/{report_id}/pages",
            headers=self.pbi_headers,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("value", [])

    def get_dataset_info(self, workspace_id: str, dataset_id: str) -> dict:
        """Get semantic model (dataset) metadata."""
        resp = requests.get(
            f"{PBI_API_BASE}/groups/{workspace_id}/datasets/{dataset_id}",
            headers=self.pbi_headers,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def get_dataset_tables(self, workspace_id: str, dataset_id: str) -> list[dict]:
        """Get tables in a semantic model via Power BI Scanner API or DAX."""
        # Try the enhanced scanner API
        resp = requests.get(
            f"{PBI_API_BASE}/groups/{workspace_id}/datasets/{dataset_id}/tables",
            headers=self.pbi_headers,
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json().get("value", [])
        return []

    def scan_workspace(self, workspace_id: str) -> dict | None:
        """Use Power BI Admin Scanner API to get detailed workspace metadata.
        
        This works without XMLA and returns tables, columns, measures, expressions.
        Requires admin permissions.
        """
        # Step 1: Trigger scan
        payload = {"workspaces": [workspace_id]}
        resp = requests.post(
            f"{PBI_API_BASE}/admin/workspaces/getInfo",
            headers=self.pbi_headers,
            json=payload,
            params={
                "lineage": True,
                "datasourceDetails": True,
                "datasetSchema": True,
                "datasetExpressions": True,
            },
            timeout=30
        )
        if resp.status_code not in (200, 202):
            console.print(f"[yellow]  Scanner API unavailable (HTTP {resp.status_code})[/yellow]")
            return None

        scan_info = resp.json()
        scan_id = scan_info.get("id")
        if not scan_id:
            return None

        # Step 2: Poll for completion
        import time
        for _ in range(30):  # max 60s
            status_resp = requests.get(
                f"{PBI_API_BASE}/admin/workspaces/scanStatus/{scan_id}",
                headers=self.pbi_headers,
                timeout=30
            )
            if status_resp.status_code != 200:
                break
            status = status_resp.json().get("status", "")
            if status == "Succeeded":
                break
            elif status in ("Failed", "NotFound"):
                return None
            time.sleep(2)

        # Step 3: Get scan result
        result_resp = requests.get(
            f"{PBI_API_BASE}/admin/workspaces/scanResult/{scan_id}",
            headers=self.pbi_headers,
            timeout=60
        )
        if result_resp.status_code == 200:
            return result_resp.json()
        return None

    def extract_model_from_scan(self, scan_data: dict, dataset_id: str) -> SemanticModelInfo | None:
        """Extract semantic model details from scanner API results."""
        if not scan_data:
            return None

        workspaces = scan_data.get("workspaces", [])
        for ws in workspaces:
            for dataset in ws.get("datasets", []):
                if dataset.get("id") == dataset_id:
                    model_name = dataset.get("name", "Unknown")
                    tables = []
                    measures = []
                    relationships = []

                    for tbl in dataset.get("tables", []):
                        cols = [
                            col.get("name", "")
                            for col in tbl.get("columns", [])
                            if col.get("name")
                        ]
                        partitions = []
                        for src in tbl.get("source", []) if isinstance(tbl.get("source"), list) else [tbl.get("source", {})]:
                            if src and isinstance(src, dict):
                                partitions.append(Partition(
                                    name="default",
                                    source_type=src.get("type", ""),
                                    source_expression=src.get("expression", ""),
                                ))

                        tables.append(TableInfo(
                            name=tbl.get("name", ""),
                            columns=cols,
                            is_hidden=tbl.get("isHidden", False),
                            partitions=partitions,
                        ))

                        # Extract measures from table
                        for m in tbl.get("measures", []):
                            measures.append(MeasureInfo(
                                name=m.get("name", ""),
                                expression=m.get("expression", ""),
                                table=tbl.get("name", "")
                            ))

                    # Extract expressions (Power Query M code)
                    for expr in dataset.get("expressions", []):
                        # Expressions are named queries (shared expressions)
                        pass  # Could be added as metadata

                    # Data sources from scan
                    data_sources = []
                    for ds in ws.get("datasources", []):
                        # Match to this dataset via datasourceInstances
                        data_sources.append(DataSource(
                            source_type=ds.get("datasourceType", "Unknown"),
                            name=ds.get("datasourceId", ""),
                            connection_string=json.dumps(ds.get("connectionDetails", {})),
                            details=ds.get("connectionDetails", {})
                        ))

                    return SemanticModelInfo(
                        name=model_name,
                        model_id=dataset_id,
                        tables=tables,
                        measures=measures,
                        relationships=relationships,
                        data_sources=data_sources
                    )
        return None

    def execute_dax(self, workspace_id: str, dataset_id: str, dax_query: str) -> dict:
        """Execute a DAX query against a semantic model."""
        payload = {
            "queries": [{"query": dax_query}],
            "serializerSettings": {"includeNulls": True}
        }
        resp = requests.post(
            f"{PBI_API_BASE}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries",
            headers=self.pbi_headers,
            json=payload,
            timeout=60
        )
        if resp.status_code == 400:
            # Log the actual error for diagnostics
            try:
                err_detail = resp.json()
                err_msg = err_detail.get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            raise RuntimeError(f"DAX query failed (400): {err_msg}")
        resp.raise_for_status()
        return resp.json()

    def get_model_tables_via_dax(self, workspace_id: str, dataset_id: str) -> list[TableInfo]:
        """Get table metadata using INFO DAX functions."""
        dax = """
        EVALUATE
        SELECTCOLUMNS(
            INFO.TABLES(),
            "TableName", [Name],
            "IsHidden", [IsHidden],
            "Description", [Description]
        )
        """
        try:
            result = self.execute_dax(workspace_id, dataset_id, dax)
            rows = self._extract_dax_rows(result)
            return [
                TableInfo(
                    name=row.get("[TableName]", ""),
                    is_hidden=row.get("[IsHidden]", False)
                )
                for row in rows
            ]
        except Exception as e:
            console.print(f"[yellow]Warning: Could not query tables via DAX: {e}[/yellow]")
            return []

    def get_model_columns_via_dax(self, workspace_id: str, dataset_id: str) -> dict[str, list[str]]:
        """Get columns per table using INFO DAX functions."""
        dax = """
        EVALUATE
        SELECTCOLUMNS(
            INFO.COLUMNS(),
            "TableName", [TableName],
            "ColumnName", [ExplicitName],
            "DataType", [DataType],
            "IsHidden", [IsHidden]
        )
        """
        try:
            result = self.execute_dax(workspace_id, dataset_id, dax)
            rows = self._extract_dax_rows(result)
            columns_by_table = {}
            for row in rows:
                table = row.get("[TableName]", "")
                col = row.get("[ColumnName]", "")
                if table and col:
                    columns_by_table.setdefault(table, []).append(col)
            return columns_by_table
        except Exception as e:
            console.print(f"[yellow]Warning: Could not query columns via DAX: {e}[/yellow]")
            return {}

    def get_model_measures_via_dax(self, workspace_id: str, dataset_id: str) -> list[MeasureInfo]:
        """Get measures using INFO DAX functions."""
        dax = """
        EVALUATE
        SELECTCOLUMNS(
            INFO.MEASURES(),
            "MeasureName", [Name],
            "TableName", [TableName],
            "Expression", [Expression]
        )
        """
        try:
            result = self.execute_dax(workspace_id, dataset_id, dax)
            rows = self._extract_dax_rows(result)
            return [
                MeasureInfo(
                    name=row.get("[MeasureName]", ""),
                    expression=row.get("[Expression]", ""),
                    table=row.get("[TableName]", "")
                )
                for row in rows
            ]
        except Exception as e:
            console.print(f"[yellow]Warning: Could not query measures via DAX: {e}[/yellow]")
            return []

    def get_model_relationships_via_dax(self, workspace_id: str, dataset_id: str) -> list[RelationshipInfo]:
        """Get relationships using INFO DAX functions."""
        dax = """
        EVALUATE
        SELECTCOLUMNS(
            INFO.RELATIONSHIPS(),
            "FromTable", [FromTableName],
            "FromColumn", [FromColumnName],
            "ToTable", [ToTableName],
            "ToColumn", [ToColumnName],
            "Cardinality", [Cardinality],
            "CrossFilteringBehavior", [CrossFilteringBehavior]
        )
        """
        try:
            result = self.execute_dax(workspace_id, dataset_id, dax)
            rows = self._extract_dax_rows(result)
            cardinality_map = {0: "None", 1: "OneToOne", 2: "ManyToOne", 3: "ManyToMany"}
            crossfilter_map = {1: "Single", 2: "Both"}
            return [
                RelationshipInfo(
                    from_table=row.get("[FromTable]", ""),
                    from_column=row.get("[FromColumn]", ""),
                    to_table=row.get("[ToTable]", ""),
                    to_column=row.get("[ToColumn]", ""),
                    cardinality=cardinality_map.get(row.get("[Cardinality]", 0), "Unknown"),
                    cross_filter=crossfilter_map.get(row.get("[CrossFilteringBehavior]", 1), "Single")
                )
                for row in rows
            ]
        except Exception as e:
            console.print(f"[yellow]Warning: Could not query relationships via DAX: {e}[/yellow]")
            return []

    def get_partitions_via_dax(self, workspace_id: str, dataset_id: str) -> dict[str, list[Partition]]:
        """Get partition source info — reveals underlying data sources."""
        dax = """
        EVALUATE
        SELECTCOLUMNS(
            INFO.PARTITIONS(),
            "TableName", [TableName],
            "PartitionName", [Name],
            "SourceType", [SourceType],
            "QueryDefinition", [QueryDefinition],
            "Mode", [Mode]
        )
        """
        try:
            result = self.execute_dax(workspace_id, dataset_id, dax)
            rows = self._extract_dax_rows(result)
            mode_map = {0: "Import", 1: "DirectQuery", 2: "Dual", 3: "Push", 4: "DirectLake"}
            partitions_by_table = {}
            for row in rows:
                table = row.get("[TableName]", "")
                partition = Partition(
                    name=row.get("[PartitionName]", ""),
                    source_type=str(row.get("[SourceType]", "")),
                    source_expression=row.get("[QueryDefinition]", "") or "",
                    storage_mode=mode_map.get(row.get("[Mode]", 0), "Unknown")
                )
                partitions_by_table.setdefault(table, []).append(partition)
            return partitions_by_table
        except Exception as e:
            console.print(f"[yellow]Warning: Could not query partitions via DAX: {e}[/yellow]")
            return {}

    def get_datasources(self, workspace_id: str, dataset_id: str) -> list[dict]:
        """Get data sources for a semantic model."""
        resp = requests.get(
            f"{PBI_API_BASE}/groups/{workspace_id}/datasets/{dataset_id}/datasources",
            headers=self.pbi_headers,
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json().get("value", [])
        return []

    def trace_report_lineage(self, workspace_id: str, workspace_name: str, report: dict) -> ReportInfo:
        """Trace full lineage for a single report."""
        report_id = report["id"]
        report_name = report.get("name") or report.get("displayName", "Unknown")

        console.print(f"\n[bold cyan]Tracing lineage for:[/bold cyan] {report_name}")

        # Get report details (includes datasetId binding)
        details = self.get_report_details(workspace_id, report_id)
        dataset_id = details.get("datasetId")

        # Get report pages
        try:
            pages_data = self.get_report_pages(workspace_id, report_id)
            pages = [p.get("displayName", p.get("name", "")) for p in pages_data]
        except Exception:
            pages = []

        report_info = ReportInfo(
            name=report_name,
            report_id=report_id,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            pages=pages
        )

        if not dataset_id:
            console.print("[yellow]  No semantic model bound to this report.[/yellow]")
            return report_info

        console.print(f"  [dim]Semantic Model ID: {dataset_id}[/dim]")

        # Check cache - avoid re-querying same model
        if dataset_id in self._model_cache:
            console.print(f"  [dim](cached)[/dim]")
            report_info.semantic_model = self._model_cache[dataset_id]
            return report_info

        # Get semantic model metadata
        try:
            ds_info = self.get_dataset_info(workspace_id, dataset_id)
        except Exception:
            ds_info = {}

        model_name = ds_info.get("name", "Unknown Model")
        console.print(f"  [green]-> Semantic Model:[/green] {model_name}")

        # Try DAX queries first, fall back gracefully
        tables = self.get_model_tables_via_dax(workspace_id, dataset_id)
        dax_worked = len(tables) > 0

        if dax_worked:
            columns_by_table = self.get_model_columns_via_dax(workspace_id, dataset_id)
            measures = self.get_model_measures_via_dax(workspace_id, dataset_id)
            relationships = self.get_model_relationships_via_dax(workspace_id, dataset_id)
            partitions_by_table = self.get_partitions_via_dax(workspace_id, dataset_id)

            # Enrich tables with columns and partitions
            for table in tables:
                table.columns = columns_by_table.get(table.name, [])
                table.partitions = partitions_by_table.get(table.name, [])
                if table.partitions:
                    table.storage_mode = table.partitions[0].storage_mode
            measures_list = measures
            relationships_list = relationships
        else:
            # Fallback: try Admin Scanner API for table/column/measure metadata
            console.print("  [yellow]DAX unavailable, trying Admin Scanner API...[/yellow]")
            scan_data = self.scan_workspace(workspace_id)
            scanned_model = self.extract_model_from_scan(scan_data, dataset_id)
            if scanned_model:
                console.print("  [green]Scanner API succeeded![/green]")
                tables = scanned_model.tables
                measures_list = scanned_model.measures
                relationships_list = scanned_model.relationships
            else:
                # Last resort: try REST tables endpoint
                rest_tables = self.get_dataset_tables(workspace_id, dataset_id)
                if rest_tables:
                    console.print(f"  [green]REST tables API returned {len(rest_tables)} table(s)[/green]")
                    tables = [
                        TableInfo(
                            name=t.get("name", ""),
                            columns=[c.get("name", "") for c in t.get("columns", [])],
                        )
                        for t in rest_tables
                    ]
                else:
                    console.print("  [dim]No table metadata available (XMLA disabled, no admin role). Data sources only.[/dim]")
                    tables = []
                measures_list = []
                relationships_list = []

        # Get data sources via REST (always works)
        datasources_raw = self.get_datasources(workspace_id, dataset_id)
        data_sources = [
            DataSource(
                source_type=ds.get("datasourceType", "Unknown"),
                name=ds.get("datasourceId", ""),
                connection_string=json.dumps(ds.get("connectionDetails", {})),
                details=ds.get("connectionDetails", {})
            )
            for ds in datasources_raw
        ]

        semantic_model = SemanticModelInfo(
            name=model_name,
            model_id=dataset_id,
            tables=tables,
            measures=measures_list,
            relationships=relationships_list,
            data_sources=data_sources
        )

        # Cache for reuse
        self._model_cache[dataset_id] = semantic_model
        report_info.semantic_model = semantic_model
        return report_info

    def trace_workspace(self, workspace_name: str, report_filter: str = None) -> list[ReportInfo]:
        """Trace lineage for all (or filtered) reports in a workspace."""
        ws = self.find_workspace(workspace_name)
        if not ws:
            console.print(f"[red]Workspace '{workspace_name}' not found.[/red]")
            return []

        ws_id = ws["id"]
        ws_display = ws["displayName"]
        console.print(f"\n[bold]Workspace:[/bold] {ws_display} ({ws_id})")

        reports = self.list_reports(ws_id)
        if report_filter:
            reports = [r for r in reports if report_filter.lower() in (r.get("displayName") or r.get("name", "")).lower()]

        if not reports:
            console.print("[yellow]No reports found.[/yellow]")
            return []

        console.print(f"[dim]Found {len(reports)} report(s) to trace.[/dim]")

        lineage_results = []
        for report in reports:
            result = self.trace_report_lineage(ws_id, ws_display, report)
            lineage_results.append(result)

        return lineage_results

    def _extract_dax_rows(self, result: dict) -> list[dict]:
        """Extract rows from DAX query result."""
        try:
            return result["results"][0]["tables"][0]["rows"]
        except (KeyError, IndexError):
            return []


def render_lineage_tree(reports: list[ReportInfo]) -> None:
    """Render lineage as a Rich tree in the terminal."""
    for report in reports:
        tree = Tree(f"[bold blue]📊 Report: {report.name}[/bold blue]")
        tree.add(f"[dim]ID: {report.report_id}[/dim]")
        tree.add(f"[dim]Workspace: {report.workspace_name}[/dim]")

        if report.pages:
            pages_branch = tree.add("[cyan]📄 Pages[/cyan]")
            for page in report.pages:
                pages_branch.add(page)

        if report.semantic_model:
            sm = report.semantic_model
            model_branch = tree.add(f"[bold green]🗃️ Semantic Model: {sm.name}[/bold green]")
            model_branch.add(f"[dim]ID: {sm.model_id}[/dim]")

            # Tables
            if sm.tables:
                tables_branch = model_branch.add(f"[yellow]📋 Tables ({len(sm.tables)})[/yellow]")
                for table in sm.tables:
                    if table.is_hidden:
                        continue
                    t_branch = tables_branch.add(f"[white]{table.name}[/white] [{table.storage_mode}]")
                    if table.columns:
                        cols_text = ", ".join(table.columns[:10])
                        if len(table.columns) > 10:
                            cols_text += f" ... +{len(table.columns) - 10} more"
                        t_branch.add(f"[dim]Columns: {cols_text}[/dim]")
                    if table.partitions:
                        for p in table.partitions:
                            source_preview = p.source_expression[:80] if p.source_expression else "N/A"
                            t_branch.add(f"[magenta]Source: {source_preview}[/magenta]")

            # Measures
            if sm.measures:
                measures_branch = model_branch.add(f"[cyan]📐 Measures ({len(sm.measures)})[/cyan]")
                for m in sm.measures[:20]:
                    measures_branch.add(f"{m.table}[{m.name}]")

            # Relationships
            if sm.relationships:
                rel_branch = model_branch.add(f"[blue]🔗 Relationships ({len(sm.relationships)})[/blue]")
                for r in sm.relationships:
                    rel_branch.add(
                        f"{r.from_table}[{r.from_column}] → {r.to_table}[{r.to_column}] ({r.cardinality})"
                    )

            # Data Sources
            if sm.data_sources:
                ds_branch = model_branch.add(f"[red]💾 Data Sources ({len(sm.data_sources)})[/red]")
                for ds in sm.data_sources:
                    ds_branch.add(f"[white]{ds.source_type}[/white]: {json.dumps(ds.details)}")

        console.print(tree)
        console.print()


def export_lineage_json(reports: list[ReportInfo]) -> dict:
    """Export lineage as JSON-serializable dict."""
    output = []
    for report in reports:
        entry = {
            "report": {
                "name": report.name,
                "id": report.report_id,
                "workspace": report.workspace_name,
                "workspaceId": report.workspace_id,
                "pages": report.pages,
            }
        }
        if report.semantic_model:
            sm = report.semantic_model
            entry["report"]["semanticModel"] = {
                "name": sm.name,
                "id": sm.model_id,
                "tables": [
                    {
                        "name": t.name,
                        "columns": t.columns,
                        "storageMode": t.storage_mode,
                        "isHidden": t.is_hidden,
                        "partitions": [
                            {
                                "name": p.name,
                                "sourceType": p.source_type,
                                "sourceExpression": p.source_expression,
                                "storageMode": p.storage_mode
                            }
                            for p in t.partitions
                        ]
                    }
                    for t in sm.tables
                ],
                "measures": [
                    {"name": m.name, "table": m.table, "expression": m.expression}
                    for m in sm.measures
                ],
                "relationships": [
                    {
                        "from": f"{r.from_table}[{r.from_column}]",
                        "to": f"{r.to_table}[{r.to_column}]",
                        "cardinality": r.cardinality,
                        "crossFilter": r.cross_filter
                    }
                    for r in sm.relationships
                ],
                "dataSources": [
                    {"type": ds.source_type, "details": ds.details}
                    for ds in sm.data_sources
                ]
            }
        output.append(entry)
    return {"lineage": output}


def main():
    parser = argparse.ArgumentParser(description="Fabric Object Lineage Tracker")
    parser.add_argument("--workspace", "-w", required=True, help="Workspace name to scan")
    parser.add_argument("--report", "-r", help="Filter to specific report name (partial match)")
    parser.add_argument("--output", "-o", help="Output JSON file path")
    parser.add_argument("--list-workspaces", action="store_true", help="List all accessible workspaces")
    args = parser.parse_args()

    tracker = FabricLineageTracker()

    if args.list_workspaces:
        workspaces = tracker.list_workspaces()
        table = Table(title="Accessible Workspaces")
        table.add_column("Name")
        table.add_column("ID")
        table.add_column("Type")
        for ws in workspaces:
            table.add_row(ws["displayName"], ws["id"], ws.get("type", ""))
        console.print(table)
        return

    results = tracker.trace_workspace(args.workspace, args.report)

    if not results:
        sys.exit(1)

    # Render tree view
    render_lineage_tree(results)

    # Export JSON if requested
    if args.output:
        lineage_data = export_lineage_json(results)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(lineage_data, f, indent=2, ensure_ascii=False)
        console.print(f"\n[green]Lineage exported to {args.output}[/green]")


if __name__ == "__main__":
    main()
