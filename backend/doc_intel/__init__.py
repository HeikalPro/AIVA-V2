"""Document intelligence layer (see docs/document-intelligence-plan.md).

Phase 1: admin knowledge-document import (upload -> extraction -> chunking ->
embedding -> publishing to selected queues) and an Admin/Developer monitoring view.

Everything in this package is additive. Existing modules are imported, never
edited, and no DDL runs at startup: tables come from the reviewed migration
scripts in ``backend/doc_intel/migrations`` (``python -m backend.doc_intel.migrate``).
"""
