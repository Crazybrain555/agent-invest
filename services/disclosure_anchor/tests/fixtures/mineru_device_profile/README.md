# Deterministic API device transition fixture

`compose-pair.json` is an independently authored synthetic fixture, not a runtime
observation. It reproduces the relevant Docker Compose structure: the CPU API
omits `deploy`; the CUDA API adds the exact GPU0 device reservation and an empty
`deploy.placement` object. Other fields remain equal and give the full-object
comparison unchanged API, peer-service and network values to preserve.

The three placement cases in `scripts/windows/test_mineru_api_device_profile.ps1`
consume only this structure. Their original runtime-pair reference had SHA256
`0c84d2ed6aff96b39624c0de05488e4fd97f9ea98d370ae6e25c84bd4b1ee35f`.
Its relevant `next.services.mineru-api.deploy` object is preserved exactly here;
runtime identities, health, machine paths and proxy implementation text are not
part of this fixture. The original observation remains separate evidence.

The PowerShell test pins the fixture's SHA256 and verifies that accepting or
rejecting a transition does not mutate it. Both transition directions, empty
placement normalization, rejection of nonempty or scalar placement, preservation
of unchanged constraints, and rejection of changed/removed constraints remain
covered. This fixture is never used to claim a real installation or GPU result.
