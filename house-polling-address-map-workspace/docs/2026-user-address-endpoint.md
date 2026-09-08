# 2026 user address lookup intent

## Product intent

Expose a user-facing lookup for the 20 September 2026 State Duma election. A
person enters their home address and receives the physical address of their 2026
voting premises when the repository has a verified match.

This endpoint is intentionally 2026-only. It does not answer historical or
future-election questions and must not silently fall back to a polling-place
assignment from another election.

## Resolution pipeline

```text
user address
  -> live CEC 2026 address search and selected address ancestors
  -> pinned State Duma election 587813923
  -> CEC subjectRf + UIK number
  -> 2026 UIK address dataset
  -> physical voting-premises address
```

The final join is `(subjectRf, uik_number)`. A bare UIK number is not a valid
key because UIK numbers repeat between federal subjects. Build the serving
index from `uik-address-workspace/work/uik-addresses-2026.csv`, which retains
the canonical `subject_code`; the smaller public handoff CSV contains only a
subject label and is not the backend join artifact. The response uses
`uik_voting_address`, not a TIK address or a commission address presented as a
substitute.

The backend must pin and validate all three observed election properties before
returning a result:

- numeric election ID `587813923`;
- external ID `2b72bb97-c625-4a02-a76b-b5740c4d5f6a`;
- voting date `2026-09-20`.

A mismatch is an upstream protocol/data failure, not a no-match.

## User-visible outcomes

The endpoint has four meaningful outcomes:

1. **Resolved:** one current CEC address resolves to a subject/UIK pair and the
   2026 dataset contains a physical voting-premises address.
2. **Needs selection:** CEC returns multiple current address candidates. Return
   those candidates for the user to choose; do not guess.
3. **Address not found:** CEC returns no current candidate for the input.
4. **Polling address unavailable:** CEC returns a valid 2026 subject/UIK pair,
   but the local 2026 dataset has no verified `uik_voting_address`. Return the
   UIK identity and say that its address is unavailable; do not substitute a TIK
   or unrelated commission address.

Transport, challenge-authentication, malformed-response, and election-metadata
failures are service failures and must remain distinguishable from the four
domain outcomes above.

## Backend boundaries

- CEC is queried on demand only to resolve the user's address to the verified
  2026 subject/UIK identity.
- The physical voting-premises address comes from the repository's assembled
  2026 UIK dataset, whose coverage and provenance remain independently
  auditable.
- CEC calls use the configured proxy, bounded timeouts, caching, and application
  rate limits. The arithmetic challenge API key remains ephemeral and is never
  written to logs or raw-response storage.
- Raw apartment-qualified user queries should not be retained in ordinary
  application logs. Evidence storage contains upstream public response bodies,
  not authentication responses.

## Release condition

This is ready for an MVP endpoint using the verified live CEC protocol. A
nationwide product claim is gated by coverage of non-empty
`uik_voting_address` values in the 2026 UIK dataset. Missing coverage must remain
visible to callers rather than being inferred or filled from historical data.
