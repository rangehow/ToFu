# Internal environment provider

This independently installable adapter owns MLP/CodeLab endpoints and official
Agent SSO integration. Public SDK code must not import this distribution.
The whole bundles directory is excluded from public exports.

Keep owner, project, immutable plan and operation marker explicit. No credential
value enters tool arguments, operation records or model results. Unknown writes
reconcile without replay. Keep TLS validation and bounded I/O on both platform
HTTP and Jupyter terminal transports. No login or network work at import.
Verify the provider tests and public/export boundary tests with offline fixtures.
Live allocations require an approved exact environment plan.

Hope compute composition is owned by `hope_composition.py`; production profile
loading is owned by `hope_host.py` and `hope_profile.schema.json`. Keep the schema
packaged in the wheel. Verify binding/loading and composed-port tests before the
full provider/SDK contracts; file configuration is not live cluster acceptance.
