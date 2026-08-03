# Occult provider catalog

Hermes ships a secret-free catalog at `agent/occult/provider_catalog.json`.
It records reviewed provider candidates, official hosts, adapter hints, and
free-access classification. The catalog is not a credential store and does
not activate a network route by itself.

`hermes tarot providers` reports the catalog summary. Providers classified as
`anonymous_free`, `recurring_free`, or `temporary_credit` are allowed by the
free-routing policy when all of these conditions hold:

1. The provider does not require a payment card.
2. The user supplies an authorized credential or the provider is keyless.
3. The adapter passes validation and a minimal health check.
4. Current terms, quota, privacy, and model availability permit the request.

Card-required, retired, and unknown providers remain blocked by policy. This
prevents the router from silently spending money, creating accounts, bypassing
verification, using leaked keys, or evading quotas. A provider can therefore
be policy-allowed while still showing `awaiting_authorized_credential` until
the operator adds a legitimate credential through the protected broker.

The active route list is separate from the catalog. A catalog entry is not a
claim that the provider is reachable, free in every region, or currently
configured on the local machine.

The authenticated `GET /v1/occult/providers` endpoint returns this same
secret-free catalog with `active_route_count` and a summary. The Tarot Router
control room displays those values separately from live Minor Arcana routes,
so an operator can see what is policy-allowed, what is awaiting authorization,
and what is actually running without exposing credentials.

To explicitly activate reviewed free routes, follow [Free provider mesh](provider-mesh.md).
