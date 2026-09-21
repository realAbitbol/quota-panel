---
name: Bug report
about: A card shows an error, a provider stopped parsing, or an install did not work
title: ""
labels: bug
---

<!-- Do not paste API keys, tokens, or accounts.json. The panel's own error strings are already
     redacted for you; paste those instead of a raw provider response. -->

**What happened**

**What you expected instead**

**Which provider** (or "all of them", or "not provider-specific")

**The card's state and error text**

<!-- Each card prints the provider's HTTP status and a state such as auth_error, no_access,
     shape_unknown, network_error. /api/quota carries the same text:

     curl -s localhost:8080/api/quota | jq '.accounts[] | {id, provider, state, error, http_status}'
-->

**How you are running it**

- [ ] `docker run`
- [ ] `docker compose`
- [ ] `python3 app.py` directly
- image tag or digest:
- output of `curl -s localhost:8080/api/health`:

**Anything else**
