"""Attachments plugin.

Owns the upload / list / delete admin surface for the
`attachments` table, plus the public delivery route that serves
attachment bytes. The Attachment model itself lives in
`bragi.core.models.attachment`; the local-disk storage helper
lives in `bragi.core.storage`. Both are reserved namespaces for
future expansion (S3 backend, renditions, alt-text bulk editing
as `bragi.contrib.media`).
"""
