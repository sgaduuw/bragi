"""Audit log admin plugin.

Reads the `audit_log` table (writer lives in `bragi.core.audit`).
Mounts an admin Blueprint at `/admin/audit` with a filtered,
paginated list view. Superuser-only: the audit log is forensic
data; ordinary editors don't need it and shouldn't be able to
read what their peers did.
"""
