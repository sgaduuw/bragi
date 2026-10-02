# Bragi

Bragi is a self-hosted CMS for blogs, personal sites, and resumes.
Write in a visual editor, keep your content as Markdown, and manage
multiple sites from one installation.

The editing interface and public website run as separate apps, sharing a
SQLite database and attachment storage. Bragi is built with Python and Flask.

[Try it locally](#try-it-locally) · [Deploy it](#deploy-it) ·
[Documentation](#documentation) · [Release notes](CHANGELOG.md)

## What you can do

- **Publish posts, pages, and resumes.** Schedule posts, preview draft changes,
  and restore earlier revisions. Organize pages into a navigation tree.
- **Run several sites.** Give each site its own hostname, theme, owner,
  and collaborators.
- **Bring your content with you.** Import from Hugo, Ghost, and WordPress,
  or use a LinkedIn data export to build a resume. Export your content as
  Markdown with attachments and redirects.
- **Choose a look.** Four built-in themes support light and dark mode.
  Install or build additional themes as Python packages.
- **Make content discoverable.** Sitemaps, Atom feeds, social previews,
  structured metadata, search, and redirects are included.
- **Connect and extend.** Use webmentions and ActivityPub, embed data tables
  and charts, or add features through plugins.

Bragi is intended for a single operator managing one or more sites. Each
site has one language. It does not provide simultaneous collaborative
editing or per-post translations.

## Try it locally

You need **Python 3.14+**, **uv**, **Git**, and **make**.
Run these commands in a fresh checkout:

```sh
git clone https://github.com/sgaduuw/bragi.git
cd bragi
uv sync
uv run bragi db upgrade
uv run bragi user create --email you@example.com --display-name "Your name" --superuser
uv run bragi site create --slug blog --hostname localhost --title "My blog" \
  --canonical-url http://localhost:8002 --owner you@example.com
make dev
```

The user command prints a generated password. Use it to sign in, then
change it when prompted.

- Open the admin at **http://localhost:8001/** and sign in with the email above.
- Open your public site at **http://localhost:8002/**. Use `localhost`,
  matching the hostname you created.
- In the admin, open your site to create pages and posts. A new site starts
  with a welcome page; configure a post-index page to give posts public URLs.

`make dev` runs the admin, delivery app, and background tasks. This local
setup uses development defaults. Use the deployment guide for a public site.

## Deploy it

The repository includes a [Docker Compose example](compose.yml) using
published images from GitHub Container Registry. It runs the admin app,
public delivery app, and a background task service against a shared data volume.

For production, pin an image release, keep a persistent secret key, configure
a TLS reverse proxy, and back up the shared data. Follow the
[deployment and operations guide](docs/operations.md) for configuration.

Bragi is also available on PyPI as **`bragi-cms`**. The Python import name
is `bragi`. See [GitHub Releases](https://github.com/sgaduuw/bragi/releases)
for published versions and [the changelog](CHANGELOG.md) for changes.

## Documentation

| I want to… | Read |
| --- | --- |
| Learn about publishing, imports, exports, or datasets | [Features and content tools](docs/content.md) |
| Configure deployment, integrations, limits, or backups | [Deployment and operations](docs/operations.md) |
| Recover a site after a failed upgrade | [Backup and recovery](docs/recovery.md) |
| Build or install a theme | [Building a theme](docs/themes.md) |
| Work on Bragi or run its checks | [Development](docs/development.md) |
| Look up a configuration field or default | [Settings reference](src/bragi/settings.py) |
| Report a bug or suggest a change | [GitHub issues](https://github.com/sgaduuw/bragi/issues) |

## License

[MIT](LICENSE).
