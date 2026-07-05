# Root Makefile: delegate gates to each service. Add new services to SERVICES as they land.
SERVICES := services/disclosure_anchor

.PHONY: agent-check test $(SERVICES)

agent-check:
	@for s in $(SERVICES); do $(MAKE) -C $$s agent-check || exit 1; done

test:
	@for s in $(SERVICES); do $(MAKE) -C $$s test || exit 1; done
