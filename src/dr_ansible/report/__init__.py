"""Output formats for module reports: tables, markdown and JSON (FR-19, FR-21).

Every renderer sorts modules by FQCN and keys by name, so the same reports
always give the same text (NFR-6).
"""

from dr_ansible.report.json_report import SCHEMA_VERSION, reports_to_json
from dr_ansible.report.markdown import audit_markdown, keys_markdown
from dr_ansible.report.summary import Counts, counts
from dr_ansible.report.table import audit_table, keys_table

__all__ = [
    "SCHEMA_VERSION",
    "Counts",
    "audit_markdown",
    "audit_table",
    "counts",
    "keys_markdown",
    "keys_table",
    "reports_to_json",
]
