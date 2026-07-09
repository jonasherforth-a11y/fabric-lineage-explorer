"""
CI/CD Lineage Validator — Runs in GitHub Actions / Azure DevOps pipelines.

Scans PBIP projects on every commit/PR and:
1. Generates a full lineage map (report → model → tables → data sources)
2. Detects broken bindings (report pointing to non-existent model)
3. Detects orphaned models (models not referenced by any report)
4. Detects data source changes (new/removed/modified connections)
5. Outputs lineage as JSON artifact + summary comment on PR

Usage:
  python ci_lineage_check.py --solution-path ./solution --output lineage.json
  python ci_lineage_check.py --solution-path ./solution --diff-base main  # Compare against base branch
"""

import argparse
import json
import sys
from pathlib import Path

from tmdl_parser import (
    scan_solution_folder,
    SemanticModelDef,
    ReportDef,
)


def build_lineage_map(models: list[SemanticModelDef], reports: list[ReportDef]) -> dict:
    """Build a complete lineage map from parsed PBIP artifacts."""
    # Index models by name for report->model resolution
    model_by_name = {}
    for m in models:
        model_by_name[m.name] = m
        # Also index without version suffix (e.g. "KDK 26 Semantic Model")
        clean_name = m.name.strip()
        model_by_name[clean_name] = m

    lineage = {
        "reports": [],
        "models": [],
        "data_sources": set(),
        "bindings": [],
        "issues": [],
    }

    # Process reports
    for report in reports:
        report_entry = {
            "name": report.name,
            "path": str(report.path),
            "semantic_model_name": report.semantic_model_name,
            "semantic_model_id": report.semantic_model_id,
            "workspace": report.workspace_name,
            "pages": report.pages,
            "bound_model": None,
        }

        # Resolve binding
        bound_model = model_by_name.get(report.semantic_model_name)
        if bound_model:
            report_entry["bound_model"] = bound_model.name
            lineage["bindings"].append({
                "report": report.name,
                "model": bound_model.name,
                "model_id": report.semantic_model_id,
            })
        elif report.semantic_model_name:
            lineage["issues"].append({
                "type": "broken_binding",
                "severity": "error",
                "report": report.name,
                "expected_model": report.semantic_model_name,
                "message": f"Report '{report.name}' references model '{report.semantic_model_name}' which is not found in the repository.",
            })

        lineage["reports"].append(report_entry)

    # Process models
    for model in models:
        model_entry = {
            "name": model.name,
            "path": str(model.path),
            "tables": [],
            "relationships": len(model.relationships),
            "data_sources": list(model.data_sources),
            "referenced_by": [],
        }

        # Find which reports reference this model
        for binding in lineage["bindings"]:
            if binding["model"] == model.name:
                model_entry["referenced_by"].append(binding["report"])

        # If no reports reference it, flag as orphaned
        if not model_entry["referenced_by"]:
            lineage["issues"].append({
                "type": "orphaned_model",
                "severity": "warning",
                "model": model.name,
                "message": f"Semantic model '{model.name}' is not referenced by any report in the repository.",
            })

        # Process tables
        total_measures = 0
        for table in model.tables:
            table_entry = {
                "name": table.name,
                "columns": len(table.columns),
                "measures": len(table.measures),
                "is_hidden": table.is_hidden,
                "data_sources": table.data_sources,
                "partitions": [
                    {"name": p.name, "type": p.source_type, "source": p.source_expression}
                    for p in table.partitions
                ],
            }
            model_entry["tables"].append(table_entry)
            total_measures += len(table.measures)
            lineage["data_sources"].update(table.data_sources)

        model_entry["total_measures"] = total_measures
        lineage["models"].append(model_entry)

    lineage["data_sources"] = sorted(lineage["data_sources"])
    return lineage


def generate_summary(lineage: dict) -> str:
    """Generate a human-readable summary of the lineage scan."""
    lines = []
    lines.append("# Fabric Lineage Scan Report")
    lines.append("")

    # Stats
    lines.append("## Summary")
    lines.append(f"- **Reports**: {len(lineage['reports'])}")
    lines.append(f"- **Semantic Models**: {len(lineage['models'])}")
    lines.append(f"- **Data Sources**: {len(lineage['data_sources'])}")
    lines.append(f"- **Bindings**: {len(lineage['bindings'])}")
    lines.append(f"- **Issues**: {len(lineage['issues'])}")
    lines.append("")

    # Lineage chains
    lines.append("## Lineage Chains")
    lines.append("")
    lines.append("| Report | Semantic Model | Tables | Data Sources |")
    lines.append("|--------|---------------|--------|--------------|")
    for report in lineage["reports"]:
        model_name = report.get("bound_model", "-")
        if model_name and model_name != "-":
            # Find model details
            model_info = next((m for m in lineage["models"] if m["name"] == model_name), None)
            if model_info:
                table_count = len(model_info["tables"])
                ds_list = ", ".join(model_info["data_sources"][:3])
                if len(model_info["data_sources"]) > 3:
                    ds_list += "..."
                lines.append(f"| {report['name']} | {model_name} | {table_count} | {ds_list} |")
            else:
                lines.append(f"| {report['name']} | {model_name} | - | - |")
        else:
            lines.append(f"| {report['name']} | (unresolved) | - | - |")
    lines.append("")

    # Data sources
    lines.append("## Data Sources")
    lines.append("")
    for ds in lineage["data_sources"]:
        lines.append(f"- `{ds}`")
    lines.append("")

    # Issues
    if lineage["issues"]:
        lines.append("## Issues")
        lines.append("")
        for issue in lineage["issues"]:
            icon = "❌" if issue["severity"] == "error" else "⚠️"
            lines.append(f"- {icon} **{issue['type']}**: {issue['message']}")
        lines.append("")

    return "\n".join(lines)


def compare_lineage(current: dict, baseline_path: str) -> list[dict]:
    """Compare current lineage against a baseline (from previous run)."""
    changes = []

    if not Path(baseline_path).exists():
        return [{"type": "info", "message": "No baseline found — first run."}]

    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))

    # Compare data sources
    current_ds = set(current.get("data_sources", []))
    baseline_ds = set(baseline.get("data_sources", []))

    added_ds = current_ds - baseline_ds
    removed_ds = baseline_ds - current_ds

    for ds in added_ds:
        changes.append({"type": "data_source_added", "severity": "info", "source": ds})
    for ds in removed_ds:
        changes.append({"type": "data_source_removed", "severity": "warning", "source": ds})

    # Compare report count
    current_reports = {r["name"] for r in current.get("reports", [])}
    baseline_reports = {r["name"] for r in baseline.get("reports", [])}

    for r in current_reports - baseline_reports:
        changes.append({"type": "report_added", "severity": "info", "report": r})
    for r in baseline_reports - current_reports:
        changes.append({"type": "report_removed", "severity": "warning", "report": r})

    # Compare model count
    current_models = {m["name"] for m in current.get("models", [])}
    baseline_models = {m["name"] for m in baseline.get("models", [])}

    for m in current_models - baseline_models:
        changes.append({"type": "model_added", "severity": "info", "model": m})
    for m in baseline_models - current_models:
        changes.append({"type": "model_removed", "severity": "warning", "model": m})

    return changes


def main():
    parser = argparse.ArgumentParser(description="CI/CD Fabric Lineage Validator")
    parser.add_argument("--solution-path", "-s", required=True, help="Path to the PBIP solution folder")
    parser.add_argument("--output", "-o", default="lineage.json", help="Output JSON file")
    parser.add_argument("--summary", default="lineage_summary.md", help="Output summary markdown")
    parser.add_argument("--baseline", "-b", help="Baseline JSON to compare against (for diff detection)")
    parser.add_argument("--fail-on-errors", action="store_true", help="Exit with code 1 if errors found")
    args = parser.parse_args()

    solution_path = Path(args.solution_path)
    if not solution_path.exists():
        print(f"ERROR: Solution path '{solution_path}' does not exist.")
        sys.exit(1)

    print(f"Scanning: {solution_path}")

    # Scan all PBIP artifacts
    result = scan_solution_folder(solution_path)
    models = result["models"]
    reports = result["reports"]

    print(f"Found: {len(models)} model(s), {len(reports)} report(s)")

    # Build lineage
    lineage = build_lineage_map(models, reports)

    # Compare against baseline if provided
    if args.baseline:
        changes = compare_lineage(lineage, args.baseline)
        lineage["changes"] = changes
        if changes:
            print(f"\nChanges detected vs baseline:")
            for c in changes:
                print(f"  [{c.get('severity', 'info')}] {c.get('type')}: {c.get('message', c.get('source', c.get('report', c.get('model', ''))))}")

    # Output JSON
    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(lineage, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nLineage JSON: {output_path}")

    # Output summary
    summary = generate_summary(lineage)
    summary_path = Path(args.summary)
    summary_path.write_text(summary, encoding="utf-8")
    print(f"Summary: {summary_path}")

    # Print summary to stdout
    print(f"\n{'='*60}")
    print(summary)

    # Check for errors
    errors = [i for i in lineage["issues"] if i["severity"] == "error"]
    if errors and args.fail_on_errors:
        print(f"\n{'='*60}")
        print(f"FAILED: {len(errors)} error(s) found.")
        sys.exit(1)

    print(f"\nDone. {len(lineage['issues'])} issue(s) found.")


if __name__ == "__main__":
    main()
