# Security Policy

## Supported Versions

The following versions of Agent System receive security updates:

| Version | Supported          |
|---------|--------------------|
| 0.3.x   | :white_check_mark: |
| 0.2.x   | :white_check_mark: |
| 0.1.x   | :white_check_mark: |
| < 0.1.0 | :x:                |

We commit to providing security fixes for the latest minor release line.

## Reporting a Vulnerability

We take security vulnerabilities seriously. We appreciate your efforts to responsibly disclose your findings.

### How to Report

**Please do NOT report security vulnerabilities through public GitHub issues.**

Instead, please report them privately via one of these channels:

1. **GitHub Security Advisories** (preferred)
   Go to https://github.com/liwt2010/all-agents/security/advisories/new
   and submit a private security advisory.

2. **Email**
   Send details to: security@agentsystem.example.com
   (Replace with actual maintainer email before publishing.)

### What to Include

Please include the following information to help us triage:

- **Type of issue** (e.g. SQL injection, XSS, authentication bypass, RCE)
- **Affected versions** (e.g. v0.1.0)
- **Affected components** (e.g. `agent_system.api.server`, specific endpoint)
- **Attack scenario** (steps to reproduce)
- **Impact assessment** (what data/systems are at risk)
- **Proof of concept** (code snippet, screenshot, or video)

### What to Expect

- **Acknowledge receipt** within 48 hours
- **Triage and assess severity** within 5 business days
- **Provide an initial fix timeline** within 10 business days
- **Coordinate disclosure** with you on a public CVE if applicable

### Severity Classification

We use CVSS v3.1 scoring:

| Severity     | CVSS Score | Examples |
|--------------|------------|----------|
| Critical     | 9.0-10.0   | RCE, auth bypass with no user interaction |
| High         | 7.0-8.9    | Privilege escalation, sensitive data exposure |
| Medium       | 4.0-6.9    | Stored XSS, limited information disclosure |
| Low          | 0.1-3.9    | Reflected XSS, debug info exposure |

### Our Commitments

- We will not pursue legal action against researchers who follow this policy
- We will credit reporters in the security advisory (unless anonymity is requested)
- We will keep you informed of our progress throughout the process

## Security Best Practices for Deployers

### Required

- **Rotate `AUTH_SECRET` every 90 days** (or use `AUTH_SECRETS` multi-key rotation)
- **Use TLS** in production (`TLS_REDIRECT_ENABLED=true`)
- **Restrict CORS origins** to trusted domains (never `*` in production)
- **Run as non-root user** in containers
- **Mount `/data` as a persistent volume** for graph/audit/backup data

### Recommended

- **Enable rate limiting** (`AGENT_RATE_LIMIT_ENABLED=true`, default 120 req/min/user)
- **Use external secret manager** (e.g. HashiCorp Vault, AWS Secrets Manager) instead of `.env`
- **Enable OpenTelemetry** for full request tracing (`AGENT_OTEL_ENABLED=true`)
- **Enable audit log retention** for compliance (default 90 days)
- **Monitor `/metrics`** for anomalies (rate limit hits, 5xx spikes)
- **Run `pip-audit`** weekly to check for new CVEs in dependencies

### Restricted File Access

The sandboxed filesystem (`ALLOWED_FILE_ROOTS`) is critical for security:

```bash
# Production example:
ALLOWED_FILE_ROOTS=data,tmp

# Do NOT include project root in production
# or agents could read/exfiltrate source code
```

### LLM API Key Handling

- Never commit `.env` to git (already in `.gitignore`)
- Use `SecretsInRequestMiddleware` to block accidental API key leaks in HTTP bodies
- Rotate `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` periodically
- Use a proxy with rate limiting and per-tenant budgets

## Security Features Built-In

Agent System v0.1.0 ships with:

- **JWT secret rotation** (`AUTH_SECRETS="kid:secret,..."` multi-key)
- **Sliding-window rate limit** (per-user, per-scope)
- **Input sanitizer** (prompt injection detection with `TrustLevel`)
- **Secrets-in-request** middleware (blocks API key leaks)
- **Request size cap** (1 MB default)
- **TLS redirect + HSTS** (production mode)
- **Audit log** with queryable HTTP endpoint
- **CSRF protection** via SameSite cookies + CORS origin check
- **Auth middleware** extracts tenant context, enforces RBAC

## Acknowledgments

We thank the following security researchers for responsibly disclosing vulnerabilities:

_(none yet — be the first!)_

## Contact

- **Security issues**: security@agentsystem.example.com (preferred: GitHub Security Advisories)
- **General questions**: GitHub Discussions or Issues
