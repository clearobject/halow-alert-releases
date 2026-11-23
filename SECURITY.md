# Security policy

## Reporting a vulnerability

Please report security vulnerabilities **privately**, through GitHub's private vulnerability reporting for this repository:

1. Open the [**Security** tab](https://github.com/clearobject/halow-alert-releases/security) of this repository.
2. Choose **Report a vulnerability** (or go straight to [the report form](https://github.com/clearobject/halow-alert-releases/security/advisories/new)).

If the form is unavailable, ask your ClearObject contact for a private channel. Do not use a public issue.

**Do not open a public issue, discussion or pull request for a vulnerability.** If you are unsure whether something is a vulnerability, report it privately anyway.

A useful report includes:

- the halow-alert version (setup A: `docker run --rm ghcr.io/clearobject/halow-alert:<tag> --version`; the Python package: `halow-client --version`; otherwise the release you downloaded) and the setup (A: alert gateway, or B: direct mode);
- what an attacker could do, and from where (your network, the HaLowLink network, the same host);
- the steps to reproduce it, ideally against the stand-in wearable ([`tools/fake_wearable.py`](tools/fake_wearable.py)) rather than real hardware.

Do not include real tokens, passwords or HaLow network passphrases, even in a private report.

We will confirm that we received your report, keep you informed while we investigate, and credit you in the advisory if you wish. Please give us reasonable time to release a fix before you disclose the issue publicly.

## Scope

In scope:

- the container image `ghcr.io/clearobject/halow-alert` and its HTTP API;
- the code in [`integrations/`](integrations/) and [`tools/`](tools/);
- [`docker-compose.yml`](docker-compose.yml), [`.env.example`](.env.example) and [`setup-network-linux.sh`](setup-network-linux.sh);
- documentation that would lead users to an insecure setup.

Problems with the ClearZone wearables themselves can be reported the same way, or to your ClearObject contact. Problems in the Morse Micro HaLowLink 2 firmware belong to Morse Micro.

## Known limitations

These are documented properties of the design, not vulnerabilities. The [security model](docs/ARCHITECTURE.md#security-model) explains them.

- The alert protocol on the HaLowLink network has no authentication: anything on `192.168.12.0/24` can trigger the wearables. Access to that network is the control.
- The HTTP API is plain HTTP. The token travels unencrypted; use the API on a trusted network and restrict TCP 8080 to your clients, or put a TLS reverse proxy in front of it.
- `GET /health` and `GET /wearables` need no token.
- A gateway started without a token (on `127.0.0.1`, or with `--allow-no-token`) accepts a plain `POST /alert` from any program on that machine (with `--allow-no-token`, from any machine that can reach the port), and from any web page open in a browser that can reach the port. Keep a token whenever a browser runs on such a machine.

## Supported versions

Security fixes are made in the **latest release** only. Pin an exact image tag (for example `1.0.0`) for stability, and move to the newest release when an advisory is published. Python package users pin the release's wheel URL or `git+https` tag, and install the newest release's wheel when an advisory is published. The [releases page](https://github.com/clearobject/halow-alert-releases/releases) lists every version.

## Hardening checklist

- Use a long random token, and keep `.env` out of version control and shared documents.
- Allow TCP 8080 on the gateway only from the machines that call the API, and TCP 5001 only from `192.168.12.0/24`.
- Keep the HaLowLink admin password and the HaLow network passphrase private.
- Leave the HaLowLink's 2.4 GHz access point off, and never connect its LAN port to another network.
- Run the image as published: host networking, no extra privileges.
