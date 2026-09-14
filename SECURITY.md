# Security

Whole-genome data is uniquely identifying and cannot be rotated like a password.

- Keep raw sequence files, VCFs, databases and findings outside Git.
- Prefer a read-only mounted input directory and a separate writable database directory.
- Use a Docker secret through `GENOMICS_API_TOKEN_FILE` in production.
- Do not expose the service directly to the public internet.
- Put the human interface behind the existing access-control layer.
- Back up encrypted data separately from encryption keys.
- Treat exported logs, reports and notifications as potentially sensitive.

Report vulnerabilities privately to the repository owner rather than opening an issue containing genomic data or credentials.

