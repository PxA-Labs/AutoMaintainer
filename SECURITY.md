# Security Policy

## Supported Versions

AutoMaintainer actively maintains and releases security patches for the latest release on the `main` branch.

| Version | Supported          |
| ------- | ------------------ |
| `main`  | :white_check_mark: |
| < 1.0   | :x:                |

---

## Reporting a Vulnerability

We take the security and integrity of AutoMaintainer very seriously. If you identify a potential security vulnerability, please help us protect our users by disclosing it responsibly.

### How to Report Privately:
1. **GitHub Private Vulnerability Reporting (Preferred):**
   * Navigate to the [Security Advisories](https://github.com/PxA-Labs/AutoMaintainer/security/advisories) tab of this repository.
   * Click **"Report a vulnerability"** to submit a private, encrypted advisory directly to maintainers.
2. **Email Security Contact:**
   * If you prefer email, send details to: `security@pxalabs.com` (or contact repository maintainers via GitHub profile).

### What to Include in Your Report:
To help us triage and remediate the issue rapidly, please include:
- A descriptive summary of the vulnerability and its potential impact.
- Step-by-step reproduction instructions or a minimal Proof of Concept (PoC).
- Affected files, functions, or dependencies.
- Any suggested remediations or patches.

---

## Response & Remediation SLA

* **Initial Acknowledgment:** Within **48 hours** of receiving the report.
* **Triage & Validation:** Within **5 business days** with an assessment of severity and remediation timeline.
* **Coordinated Disclosure:** We adhere to a standard 90-day responsible disclosure window. Once a security patch is merged and released, we will publish a security advisory crediting the reporter.

---

## Automated Security Audits & Tooling

AutoMaintainer enforces strict automated continuous security checks across all pull requests:
* **CodeQL SAST:** Deep static analysis across Python and JavaScript/TypeScript codebases.
* **Gitleaks:** Real-time secret and credential detection on every commit and PR.
* **Bandit & Pip-Audit:** Python AST security analysis and CVE vulnerability tracking against the OSV database.
* **OpenSSF Scorecard:** Continuous supply-chain and workflow security analysis.
