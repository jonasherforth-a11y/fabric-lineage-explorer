# Fabric Object Lineage Tracker — POC

## Purpose

Interactive lineage tracking from **Power BI Report** → **Semantic Model** → **Tables/Columns** → **Data Sources** (Lakehouse, SQL, etc.)

Built on top of Microsoft Fabric Skills + REST APIs. Designed to be used inside VS Code with GitHub Copilot or any agentic environment.

## Architecture

```
┌─────────────────┐     ┌──────────────────────┐     ┌────────────────────┐
│   Report Layer  │────▶│  Semantic Model Layer │────▶│  Data Source Layer  │
│                 │     │                       │     │                    │
│ - Report name   │     │ - Tables              │     │ - Partition source │
│ - Pages/Visuals │     │ - Columns             │     │ - Connection info  │
│ - Filters       │     │ - Measures            │     │ - Lakehouse/SQL    │
│ - Bindings      │     │ - Relationships       │     │ - Gateway          │
└─────────────────┘     └──────────────────────┘     └────────────────────┘
```

## How It Works

1. **Discovery** — Finds reports in your workspace via Fabric REST API
2. **Report Inspection** — Extracts semantic model binding from report definition
3. **Schema Extraction** — Queries semantic model for tables, columns, measures, relationships
4. **Source Tracing** — Uses `INFO.PARTITIONS()` DAX to resolve data sources
5. **Lineage Output** — Generates a structured JSON/Markdown lineage map

## Prerequisites

- Azure CLI authenticated (`az login`)
- Access to Fabric workspaces (Contributor or higher)
- Python 3.10+

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run interactive lineage trace
python lineage_tracker.py --workspace "Your Workspace Name"

# Trace a specific report
python lineage_tracker.py --workspace "Your Workspace" --report "Report Name"

# Output full lineage as JSON
python lineage_tracker.py --workspace "Your Workspace" --output lineage.json
```

## Copilot / Agent Usage

With Fabric Skills installed, you can also run this interactively via prompts:

```
Trace the full lineage for the "Sales Report" in workspace "Production Analytics" — 
show me report → semantic model → tables → data sources.
```

## Output Example

```json
{
  "report": {
    "name": "Sales Report",
    "id": "abc-123",
    "workspace": "Production Analytics",
    "pages": ["Overview", "Details", "Trends"],
    "semanticModel": {
      "name": "Sales Model",
      "id": "def-456",
      "tables": [
        {
          "name": "Sales",
          "columns": ["Amount", "Date", "ProductKey"],
          "partitionSource": "Lakehouse.dbo.fact_sales",
          "storageMode": "Import"
        },
        {
          "name": "Product", 
          "columns": ["ProductKey", "Name", "Category"],
          "partitionSource": "Lakehouse.dbo.dim_product",
          "storageMode": "Import"
        }
      ],
      "measures": [
        {"name": "Total Sales", "expression": "SUM(Sales[Amount])"}
      ],
      "relationships": [
        {"from": "Sales[ProductKey]", "to": "Product[ProductKey]", "cardinality": "ManyToOne"}
      ],
      "dataSources": [
        {"type": "Lakehouse", "name": "SalesLakehouse", "workspaceId": "..."}
      ]
    }
  }
}
```
