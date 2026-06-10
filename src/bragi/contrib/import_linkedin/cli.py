"""CLI for the LinkedIn importer.

Two phases:
- `bragi import linkedin <zip> --site <slug> [--page-slug cv] [--plan-out PATH]`
  generates a plan.json
- `bragi import linkedin --apply <plan-path>` reads plan.json,
  filters proposals where apply=true, runs apply().
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import click
from sqlalchemy import select

from bragi.contrib.import_linkedin.importer import apply, detect, plan
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@click.command("linkedin", help="Import a LinkedIn data export ZIP.")
@click.option("--site", "site_slug", default=None, help="Target site slug (required for planning).")
@click.option("--page-slug", default="cv", help="Target resume page slug. [default: cv]")
@click.option(
    "--plan-out",
    default="linkedin-plan.json",
    type=click.Path(dir_okay=False),
    help="Where to write the plan file when planning.",
)
@click.option(
    "--apply",
    "apply_plan_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Apply a previously-generated plan instead of planning.",
)
@click.argument("source_path", required=False, type=click.Path(exists=True, dir_okay=False))
def linkedin_command(
    site_slug: str | None,
    page_slug: str,
    plan_out: str,
    apply_plan_path: str | None,
    source_path: str | None,
) -> None:
    if apply_plan_path and source_path:
        click.echo("--apply and a source ZIP are mutually exclusive.", err=True)
        sys.exit(1)
    if apply_plan_path:
        _run_apply(Path(apply_plan_path))
        return
    if not source_path:
        click.echo("Provide a ZIP path to plan, or --apply <plan>.", err=True)
        sys.exit(1)
    if not site_slug:
        click.echo("--site is required when planning.", err=True)
        sys.exit(1)
    _run_plan(Path(source_path), site_slug, page_slug, Path(plan_out))


def _run_plan(zip_path: Path, site_slug: str, page_slug: str, plan_out: Path) -> None:
    if not detect(zip_path):
        click.echo(
            f"{zip_path} does not look like a LinkedIn export (Profile.csv missing).", err=True
        )
        sys.exit(1)

    with SessionLocal() as db:
        site = db.execute(
            select(Site).where(Site.slug == site_slug.strip().lower())
        ).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            sys.exit(1)
        page = db.execute(
            select(Page).where(
                Page.site_id == site.id,
                Page.slug == page_slug,
            )
        ).scalar_one_or_none()
        if page is not None and page.kind != PageKind.RESUME:
            click.echo(f"Page {page_slug!r} exists but is not a resume page.", err=True)
            sys.exit(1)
        # If page is None we still plan; apply will create the page.
        db.expunge_all()

    result = plan(zip_path, page)
    envelope = {
        "version": 1,
        "source_zip": str(zip_path.resolve()),
        "source_zip_sha256": _sha256_file(zip_path),
        "target": {"site_slug": site_slug, "page_slug": page_slug},
        "generated_at": datetime.now(UTC).isoformat(),
        "proposals": [dataclasses.asdict(p) for p in result.proposals],
    }
    plan_out.write_text(
        json.dumps(envelope, indent=2, sort_keys=False),
        encoding="utf-8",
    )

    by_section: dict[str, dict[str, int]] = {}
    for p in result.proposals:
        by_section.setdefault(p.section, {"add": 0, "update": 0, "remove": 0})
        by_section[p.section][p.kind] = by_section[p.section].get(p.kind, 0) + 1
    click.echo(f"Plan written to {plan_out}")
    for section, kinds in sorted(by_section.items()):
        click.echo(f"  {section}: " + ", ".join(f"{n} {k}" for k, n in kinds.items() if n))
    if not result.proposals:
        click.echo("(no changes proposed)")
    for w in result.warnings:
        click.echo(f"warn: {w}", err=True)
    click.echo(f"Review the plan, then run: bragi import linkedin --apply {plan_out}")


def _run_apply(plan_path: Path) -> None:
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    zip_path = Path(raw["source_zip"])
    if not zip_path.exists():
        click.echo(f"Plan refers to ZIP at {zip_path}, which no longer exists.", err=True)
        sys.exit(1)
    if _sha256_file(zip_path) != raw["source_zip_sha256"]:
        click.echo("Plan was generated from a different ZIP (sha256 mismatch).", err=True)
        sys.exit(1)

    site_slug = raw["target"]["site_slug"]
    page_slug = raw["target"]["page_slug"]
    selected = {p["id"] for p in raw["proposals"] if p.get("apply", True)}

    with SessionLocal() as db:
        site = db.execute(
            select(Site).where(Site.slug == site_slug.strip().lower())
        ).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            sys.exit(1)
        page = db.execute(
            select(Page).where(
                Page.site_id == site.id,
                Page.slug == page_slug,
            )
        ).scalar_one_or_none()
        if page is None:
            owner = db.execute(select(User).order_by(User.id)).scalars().first()
            if owner is None:
                click.echo("No users found in the database; create one first.", err=True)
                sys.exit(1)
            page = Page(
                site_id=site.id,
                slug=page_slug,
                title=page_slug.title(),
                author_id=owner.id,
                status=PageStatus.DRAFT,
                kind=PageKind.RESUME,
                body_markdown="",
                body_html="",
                body_excerpt="",
            )
            db.add(page)
            db.commit()
        elif page.kind != PageKind.RESUME:
            click.echo(f"Page {page_slug!r} exists but is not a resume page.", err=True)
            sys.exit(1)
        db.expunge_all()

    result = apply(zip_path, page, {"selected_change_ids": selected})
    counts = result.counts
    click.echo(
        f"Applied: {counts.get('added', 0)} added, "
        f"{counts.get('updated', 0)} updated, "
        f"{counts.get('removed', 0)} removed."
    )
    for w in result.warnings:
        click.echo(f"warn: {w}", err=True)
