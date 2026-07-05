# disclosure-anchor

A local disclosure raw-data service for financial reports, announcements, provenance tracking, parsing, storage, and downstream L2/L3 evidence pipelines.

## Phase 01 local commands

Create a local ignored virtual environment and install declared dependencies:

```bash
/opt/miniconda3/bin/python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Set service runtime paths in your shell or a private env file outside this checkout. `.env.template` and
`.env.example` show the expected disclosure_anchor variables and keep all credentials as placeholders.

Run the Phase 01 checks:

```bash
make test-unit
make doctor
make api
```

`make api` starts a local uvicorn app factory on `127.0.0.1:8711` and refuses to boot when required runtime
environment variables, the external root, or the mount sentinel are missing.

Create a clean source archive from tracked files only:

```bash
make archive
```

The archive target uses `git archive`, so ignored local state such as `.env`, `.venv`, `tmp/`, `__pycache__/`,
and large parser artifacts are not packaged.
