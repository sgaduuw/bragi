"""Form parsing keeps its own memory cap alongside the request-body cap."""

from __future__ import annotations

import pytest
from flask import Flask, request

from bragi.core.middleware.form_limits import register_form_limits


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "form_limit, body_limit, size, status",
    [
        (16, 128, 16, 200),
        (16, 128, 17, 413),
        (16, 8, 8, 200),
        (16, 8, 9, 413),
        (16, None, 16, 200),
        (16, None, 17, 413),
        (None, 128, 32, 200),
    ],
)
def test_form_limit_preserves_fields_and_smaller_body_cap(
    form_limit: int | None, body_limit: int | None, size: int, status: int, streamed: bool
) -> None:
    app = Flask(__name__)
    app.config.update(MAX_FORM_MEMORY_SIZE=form_limit, MAX_CONTENT_LENGTH=body_limit)
    register_form_limits(app)

    @app.post("/")
    def echo() -> str:
        return request.form["field"]

    body = b"field=" + b"x" * (size - len(b"field="))
    overrides = {"CONTENT_LENGTH": "", "wsgi.input_terminated": True} if streamed else {}
    response = app.test_client().post(
        "/",
        data=body,
        content_type="application/x-www-form-urlencoded; charset=utf-8",
        environ_overrides=overrides,
    )
    assert response.status_code == status
    if status == 200:
        assert response.data == b"x" * (size - len(b"field="))
