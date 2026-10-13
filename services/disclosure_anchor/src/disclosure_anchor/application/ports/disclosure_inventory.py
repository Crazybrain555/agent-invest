"""Own-service read-only inventory projection port."""

from typing import Protocol

from disclosure_anchor.application.contracts.disclosure_inventory import InventoryQuery, InventorySnapshot


class DisclosureInventoryReader(Protocol):
    def read(self, query: InventoryQuery) -> InventorySnapshot: ...
