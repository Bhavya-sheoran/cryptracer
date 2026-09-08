# TLS for the local stack

Caddy terminates TLS in front of everything. The whole system is served from
**one https origin**, which is why the dashboard calls `/api/v1/...` as a
relative path and there is no CORS configuration to get wrong.

```
https://localhost:8443        dashboard
https://localhost:8443/api    API
https://localhost:8443/docs   OpenAPI docs
```

Start it with the rest of the stack:

```bash
docker compose up -d
```

---

## The certificate warning is expected

`tls internal` issues a certificate from **Caddy's own local certificate
authority**, which lives in the `caddydata` volume. Your browser has never
heard of that CA, so on first visit it will warn that the connection is not
private.

That warning is correct behaviour, not a defect. You have two options.

### Option 1 — click through it

For a demo this is fine. Chrome: **Advanced → Proceed to localhost
(unsafe)**. Firefox: **Advanced → Accept the Risk and Continue**.

The connection is still encrypted. What the browser cannot verify is *who* it
is talking to, which on localhost you already know.

### Option 2 — trust Caddy's root CA (no more warnings)

Export the root certificate and install it in Windows' trust store:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

Then, in an **Administrator** PowerShell:

```powershell
Import-Certificate -FilePath .\caddy-root.crt `
  -CertStoreLocation Cert:\LocalMachine\Root
```

Restart the browser. The warning is gone and the padlock is solid.

To undo it later, open `certmgr.msc` → Trusted Root Certification Authorities
→ Certificates, and delete the entry issued by **Caddy Local Authority**.

> Only ever install a root CA you generated yourself, as here. A root CA can
> vouch for *any* site, so installing someone else's is handing them the
> ability to impersonate your bank.

---

## What is still http

The direct ports stay open for development convenience:

| Port | Service | Encrypted |
|---|---|---|
| 8443 | Caddy (dashboard + API) | **yes** |
| 5174 | Vite dev server | no |
| 8001 | backend | no |

**A real deployment removes the 5174 and 8001 port mappings from
`docker-compose.yml`.** Leaving a plaintext route open beside the encrypted one
makes the encryption optional in practice — an attacker simply asks for the
http port, and the tokens they are after travel in the clear.

---

## Beyond localhost

`tls internal` is for local use only. For a real hostname, replace the site
block in `infra/caddy/Caddyfile`:

```
chaintrace.example.gov.in {
    # No `tls internal` line - Caddy obtains and renews a public certificate
    # from Let's Encrypt automatically, provided port 80 and 443 reach it.
    ...
}
```

Then publish 80 and 443 instead of 8443, and raise the
`Strict-Transport-Security` max-age from the deliberately short local value
(`3600`) to a year (`31536000; includeSubDomains`) once you are confident the
certificate chain is right. That header is hard to walk back: browsers will
refuse to talk http to the host for its full lifetime.
