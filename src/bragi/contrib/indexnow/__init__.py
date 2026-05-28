"""IndexNow push-crawl integration for bragi.

IndexNow (`https://www.indexnow.org`) is the Microsoft / Yandex
protocol for telling participating search engines when a URL
has changed. Plugins subscribe to bragi's lifecycle hooks
(on_post_published / on_post_updated / on_post_deleted) and
POST the affected URL to the configured endpoint; the search
engines refetch the page on their own schedule.

The per-site key lives in `Site.extra_settings['indexnow_key']`.
Sites without a key are no-ops (the plugin doesn't fire). The
delivery app serves the key at `/<key>.txt` so the search
engines can verify ownership.

`bragi indexnow setup --site <slug>` writes a fresh random key
into a site's extra_settings.
"""
