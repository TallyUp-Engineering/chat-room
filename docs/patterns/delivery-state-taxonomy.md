# Pattern: separate delivery states

Teams lose time when “implemented,” “selected,” “observed,” and “delivered” collapse into one status.

- **Modeled**: desired behavior exists as authored intent.
- **Selected**: a source revision or candidate was chosen.
- **Observed**: a test, provider, or runtime emitted evidence about it.
- **Settled**: the exact revision is reachable through the authoritative delivery path.

Room messages may report any state, but should never silently promote one into another. A green local check is an observation, not protected-main settlement.
