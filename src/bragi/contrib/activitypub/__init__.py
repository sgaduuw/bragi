"""bragi.contrib.activitypub (#148): one ActivityPub actor per site.

Each Site becomes a follow-able actor, addressable as
`@<site-slug>@<hostname>` on the fediverse. Mastodon users
follow the actor; published posts arrive in their timeline as a
Create + Note activity with a link back to the canonical post.

Surfaces:

- `/.well-known/webfinger?resource=acct:<handle>@<host>`:
  WebFinger lookup mapping `@slug@host` to the actor URL.
- `/actor`: the actor JSON-LD document (one per site, since each
  site has its own hostname). Advertises inbox, outbox,
  followers, and the public key.
- `/actor/inbox`: signed POSTs from remote actors. Handles
  `Follow` and `Undo Follow` in v1; other activities are ACK'd
  and ignored.
- `/actor/outbox`: paginated `OrderedCollection` of past Create
  activities (one per published post).
- `bragi activitypub keygen --site SLUG`: generate the per-site
  RSA keypair (also runs automatically on first need).

Out of v1: receiving replies as comments (bragi has no comment
system; the issue made this explicit), outbound Like / Boost /
Reply, DM-style ActivityPub, multi-actor per author. The
namespace stays single-actor-per-site.
"""
