def test_attachment_rendition_quality_defaults():
    from bragi.settings import Settings

    s = Settings()
    assert s.attachment_rendition_quality_jpeg == 85
    assert s.attachment_rendition_quality_webp == 80
    assert s.attachment_rendition_quality_avif == 60


def test_attachment_rendition_worker_knobs():
    from bragi.settings import Settings

    s = Settings()
    assert s.attachment_rendition_worker_batch == 20
    assert s.attachment_rendition_max_attempts == 3
