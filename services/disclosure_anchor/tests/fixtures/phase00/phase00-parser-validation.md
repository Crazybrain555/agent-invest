# Phase 00 Parser Validation

## Environment

- macOS: 26.5.1 (25F80), arm64
- Python for MinerU: 3.13.13 at `/Volumes/AgentSSD/agent_system/services/disclosure_anchor/runtime/venvs/mineru-phase00`
- MinerU: 3.4.0, backend `pipeline`, source `modelscope`
- Model cache: `/Volumes/AgentSSD/agent_system/shared/model_cache/modelscope`
- PostgreSQL: pass via Homebrew PostgreSQL 18.4 at `/opt/homebrew/opt/postgresql@18`; PGDATA is `/Volumes/AgentSSD/agent_system/postgres/pg18-main`; the temporary conda PostgreSQL env/cache and superseded PG17 paths were removed after validation. `port=55432` and AgentSSD `unix_socket_directories` are persisted in `postgresql.conf`; a bare `pg_ctl -D <PGDATA> start` is the only sanctioned start path and `brew services` is not used. Connection posture was changed on 2026-06-28 from socket-only (`listen_addresses=''`) to localhost-only TCP (`listen_addresses='localhost'`, plus a scram password on role `disclosure_anchor`) so IDE/DBeaver clients can connect; it is loopback-only (`127.0.0.1` / `::1`) and not exposed to the LAN.

## Parser Runs

| sample | parsed pages | elements | document units | conclusion |
|---|---:|---:|---|---|
| short_announcement | 1-4 (full) | 31 | {'text': 7} | pass |
| ir_activity | 1-24 (full) | 131 | {'mixed': 2, 'text': 38} | pass (L1 evidence preservation) |
| annual_report | 1-209 (full) | 2697 | {'text': 260, 'table': 19, 'mixed': 287} | pass |

## IR v2 heading_level coverage

| sample | kind==heading elements | heading_level non-null | total elements |
|---|---:|---:|---:|
| annual_report_excerpt | 10 | 10 | 40 |
| ir_activity | 39 | 39 | 131 |
| short_announcement | 6 | 6 | 31 |
| annual_report | 655 | 655 | 2697 |

## Known Risks

- IR first Q&A is embedded in the initial activity table. L1 preserves that typed table and the subsequent ordered structure blocks without using Q&A vocabulary to rewrite boundaries; L2 performs the Q&A extraction.
- The annual-report replay groups consecutive text/table evidence under the same heading occurrence into complete structural blocks. Raw table strings and numeric source values remain provenance-bound rather than being normalized destructively.
- Annual-report heading paths combine MinerU's typed headings with generic outline/TOC structure. The golden fixture guards evidence conservation and deterministic projection; it is not a market-wide parser-quality claim.
- MinerU 3.4.0 wrote user-level `/Users/zhang/mineru.json`; it contains placeholders and external model paths, not secrets.
- Local proxy env caused MinerU/httpx to fail without `socksio`; local parse succeeds when `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` are cleared and `NO_PROXY=*` is set.

## A01-A05 State

- A01: pass, AgentSSD mounted and sentinel exists.
- A02: pass, native PostgreSQL 18.4 can initialize, start, accept socket connections, create/drop a probe database, and stop with PGDATA on AgentSSD. At validation time socket-only/55432 was persisted in `postgresql.conf` and re-verified by a bare `pg_ctl start` (both 127.0.0.1:5432 and :55432 reported no response; the socket lives under AgentSSD). (Updated 2026-06-28: connection posture was later switched to localhost-only TCP on loopback `127.0.0.1:55432` / `::1:55432` for IDE/DBeaver access — see Environment above.)
- A03: pass, model and uv caches were directed to AgentSSD.
- A04: pass, normalized IR fixtures exist for all three sample roles, including full 209-page annual report.
- A05: pass, document unit fixtures exist for all three sample roles, including full 209-page annual report.
