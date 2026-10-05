"""Keep URL-encoded forms bounded independently of the file upload allowance."""

from __future__ import annotations

from flask import Flask, abort, request


def register_form_limits(app: Flask) -> None:
    @app.before_request
    def _limit_urlencoded_form() -> None:
        limit = request.max_form_memory_size
        if request.mimetype != "application/x-www-form-urlencoded" or limit is None:
            return

        # Werkzeug 3.1.9 applies this limit only to multipart fields.
        # Read one extra byte so a streamed oversized form is rejected,
        # rather than silently parsing a truncated body at the limit.
        body_limit = request.max_content_length
        if body_limit is not None:
            limit = min(limit, body_limit)
        request.max_content_length = limit + 1
        if len(request.get_data(cache=True)) > limit:
            abort(413)
