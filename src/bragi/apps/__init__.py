"""Flask app factories for the two bragi binaries.

bragi ships as two processes:

- `bragi-admin`    (write API, editor UI, OAuth handling)
- `bragi-delivery` (read-only public renderer)

Both share `bragi.core` and the model layer; only the middleware
stacks and the set of registered Blueprints differ.
"""
