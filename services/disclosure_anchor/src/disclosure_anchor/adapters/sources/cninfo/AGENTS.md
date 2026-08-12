# CNInfo source-adapter boundary

- Implement CNInfo access behind the application source port. Credentials and channel selection are injected;
  query parameters, logs, fixtures, and review output are redacted before persistence or display.
- Preserve provider document identity, access time/result, channel, source metadata, raw content hash, and the
  distinction between provider facts and title-derived classifications.
- API and web channels may have different capabilities. Map absence explicitly; do not fabricate profiles,
  category codes, periods, sizes, or issuer identity to make the channels look equivalent.
- Rate limits, token refresh, retry/backoff, non-JSON responses, and response mapping follow the deployed provider
  contract and typed failure policy. Revalidate mechanism changes against official/provider evidence and a
  representative frozen corpus.
- Classification rules are versioned data. Ordering/precedence is semantic, hard-negative rules require evidence
  that no new financial fact is lost, and single-issuer/single-event phrases do not become global rules.
- Before changing a vocabulary, follow `docs/implementation/design/classification-facets-and-derived-views.md`,
  the research workflow, and the relevant review history indexed by `docs/implementation/README.md`: review
  in-pool and out-of-pool matches, an unused holdout, provider-code/title disagreement, and adjacent negatives.
- Registration remains broader than processing eligibility so later rule changes can replay preserved candidates.
  Do not bypass the shared registration/source-access pipeline.
- Current rule versions, dated audits, retry constants, corpus counts, issuer examples, and historical rounds belong
  in versioned rule assets, review/checklist artifacts, or milestone history—not in agent instructions.
