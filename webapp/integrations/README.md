# Integrations

Each external integration lives in its own Python package:

```text
integrations/
  cdb/
    __init__.py
    cdb_mappings.yaml
    README.md
  discord/
    __init__.py
    README.md
  firstcycling/
    api.py
    README.md
  activity_monitor_queue.py
  pcs/
    __init__.py
    README.md
```

FirstCycling exposes its client and API error through `firstcycling/api.py`:
`from webapp.integrations.firstcycling.api import FirstCyclingClient, FirstCyclingAPIError`.
Additional Python modules, Markdown documentation, YAML mappings, and other
resources can live beside it as needed.

## Supported integrations

- [CDB](cdb/README.md): PCM SQLite inspection, updates, and field mappings.
- [Discord](discord/README.md): workflow notifications and approval images.
- [FirstCycling](firstcycling/README.md): API fetching and main database sync.
- [ProCyclingStats](pcs/README.md): result HTML parsing, Namespace CDB rider matching, and daily checks for unconfirmed rider profile slugs.
- `activity_monitor_queue.py`: retirement and audit-event handling for namespace riders. The former race-activity collection workflow remains retired; daily PCS slug identity checks are scheduled separately.

## Adding an integration

Create a package directory with `__init__.py` and a `README.md` describing its
purpose, configuration, usage, and persistence behavior. Keep public imports
available from the package if its implementation is later split into modules.

Add YAML files when needed; files are not automatically loaded as configuration.
Keep credentials and sensitive URL segments in environment variables, never in
committed resources. Encapsulate network and persistence behavior and redact
secrets from errors.
