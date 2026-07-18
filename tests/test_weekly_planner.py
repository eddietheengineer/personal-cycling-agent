"""
Tests for weekly planner and UI rendering.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


def _read_source(name: str) -> str:
    return (SRC / name).read_text()


def _collect_fstring_names(source: str) -> set[str]:
    """Extract all variable names used inside f-strings."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    v = value.value
                    if isinstance(v, ast.Name):
                        names.add(v.id)
                    elif isinstance(v, ast.Attribute):
                        root = v
                        while isinstance(root, ast.Attribute):
                            root = root.value
                        if isinstance(root, ast.Name):
                            names.add(root.id)
    return names


def _collect_local_names(source: str) -> set[str]:
    """Collect all names assigned/imported in the module."""
    def _extract_names(target):
        """Recursively extract Name nodes from assignment targets (handles tuples)."""
        if isinstance(target, ast.Name):
            return {target.id}
        elif isinstance(target, (ast.Tuple, ast.List)):
            names = set()
            for elt in target.elts:
                names.update(_extract_names(elt))
            return names
        return set()

    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.FunctionDef):
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                names.add(arg.arg)
            for child in ast.walk(node):
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        names.update(_extract_names(target))
                elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    names.add(child.target.id)
                elif isinstance(child, ast.For):
                    names.update(_extract_names(child.target))
                elif isinstance(child, ast.With):
                    if child.items and child.items[0].optional_vars:
                        ov = child.items[0].optional_vars
                        names.update(_extract_names(ov))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_extract_names(target))
    return names


@pytest.mark.parametrize(
    "module_path",
    [
        "visualize.py",
        "analytics/weekly_planner.py",
        "services/weather.py",
        "config/schedule.py",
    ],
)
def test_no_undefined_names_in_fstrings(module_path: str):
    """Catch NameError/UnboundLocalError bugs in f-strings before runtime."""
    source = _read_source(module_path)
    used = _collect_fstring_names(source)
    defined = _collect_local_names(source)
    allowed = {
        "st", "json", "os", "math", "logging", "Path", "date", "timedelta",
        "range", "len", "set", "list", "dict", "tuple", "str", "int", "float",
        "True", "False", "None", "Exception", "RuntimeError", "ValueError",
        "KeyError", "IndexError", "TypeError", "AttributeError",
        "go", "pd", "numpy", "np", "plt", "logger",
        # Exception handlers
        "e", "exc", "err",
        # Common loop/comprehension vars
        "k", "v", "i", "j", "idx", "item", "row", "col", "entry",
        # Streamlit/plotly
        "page_id", "label", "fig", "trace",
        # DB/config
        "db_path", "db", "cursor", "conn", "unit",
        # Map
        "center_lat", "center_lon",
        # Weather
        "start_hour", "slot_note", "ride_duration", "ride_duration_hours",
        # Projection chart
        "last_ctl", "last_atl", "last_tsb", "ctl_s", "atl_s", "tsb_s",
        "ctl_change", "tsb_change", "ctl_status", "atl_status", "tsb_status",
        "tl", "analysis", "daily_tss", "c_cols",
    }
    undefined = used - defined - allowed
    assert not undefined, f"Undefined names in f-strings ({module_path}): {undefined}"


def test_projection_values_reasonable():
    """CTL changes slowly, ATL decays faster, TSB = CTL - ATL."""
    from src.analytics.weekly_planner import generate_weekly_plan
    plan = generate_weekly_plan()
    assert len(plan.ctl_series) == 7, "Should project 7 days"
    assert len(plan.atl_series) == 7
    assert len(plan.tsb_series) == 7
    for i in range(1, len(plan.ctl_series)):
        prev = plan.ctl_series[i - 1]
        curr = plan.ctl_series[i]
        if prev > 0:
            change = abs(curr - prev) / prev
            assert change < 0.15, f"CTL changed {change:.1%} on day {i}"


def test_training_days_have_descriptions():
    """Training days must have non-empty descriptions."""
    from src.analytics.weekly_planner import generate_weekly_plan
    plan = generate_weekly_plan()
    for day in plan.days:
        if not day.rest_day:
            assert day.description, f"{day.date} missing description"