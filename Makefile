# Root Makefile: delegate gates to each component. Add new services/packages to the lists as they land.
SERVICES := services/disclosure_anchor
PACKAGES := packages/envelope_kernel
COMPONENTS := $(SERVICES) $(PACKAGES)

.PHONY: agent-check test

agent-check:
	@for c in $(COMPONENTS); do $(MAKE) -C $$c agent-check || exit 1; done

test:
	@for c in $(COMPONENTS); do $(MAKE) -C $$c test || exit 1; done
