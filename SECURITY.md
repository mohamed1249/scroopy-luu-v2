# Security and supported scope

This early release is intended for a trusted user's local machine, bound to loopback.
It is not a hardened public or multi-tenant service. Use a private random API key and
do not share the MongoDB URI or enable public network access as a workaround.

HTTP scraping checks destinations and redirects and rejects local/private addresses.
These checks do not replace network isolation, access control or operational quotas.
The optional legacy Google Drive route is outside the new pull-storage contract.

Report sensitive vulnerabilities through GitHub's private vulnerability reporting
if available. Do not put credentials, personal scraped content or working exploit
details in public issues. For ordinary bugs, include a minimal public fixture and
sanitized reproduction steps. No response-time guarantee is offered.
