# RingBack migration & status-sync notes

Operational notes for state that changes after deploy — chiefly the A2P 10DLC
status sync that drives go-live.

## A2P status sync

A2P status sync can downgrade. `connections.a2p_sync` reflects Twilio's current
`campaign_status` exactly, including the bad direction: an `approved` business is
moved back to `failed`/`pending` if Twilio later reports SUSPENDED, DELETED,
EXPIRED, or FAILED. This is intentional — it re-blocks go-live (`is_live` becomes
False) so RingBack never claims "live" for a campaign that died at the
carrier/registry. Terminal-bad upstream states map to `failed`; in-flight (incl.
REGISTERED) maps to `pending`; only VERIFIED/APPROVED grant `approved`.
