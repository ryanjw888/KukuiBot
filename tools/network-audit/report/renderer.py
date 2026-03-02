"""renderer.py — Report rendering entry point.

Delegates to generator.py for the branded Kukui IT HTML report.
Kept as a thin wrapper for backwards compatibility with audit.py.
"""

import json
from pathlib import Path

from .generator import generate_report


def render_report(
    scan_data: dict,
    analysis: dict,
    output_path: Path | str,
) -> Path:
    """Generate the final HTML report from scan data and AI analysis.

    Args:
        scan_data: Parsed scan_results.json dict
        analysis: Parsed analysis.json dict (AI-generated findings)
        output_path: File path or directory for the output

    Returns:
        Path to the generated HTML file
    """
    output_path = Path(output_path)

    # If output_path looks like a directory, put the report inside it
    if output_path.is_dir() or not output_path.suffix:
        output_dir = output_path
        filename = "network_security_report.html"
    else:
        output_dir = output_path.parent
        filename = output_path.name

    return generate_report(scan_data, analysis, output_dir, filename)
