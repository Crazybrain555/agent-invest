"""Load actual standalone bytes in an isolated MinerU namespace, no MinerU startup."""

import importlib.util
from pathlib import Path
import sys
import types


class CapacityBootstrapFixture:
    def __init__(self, root: Path):
        service = Path(__file__).resolve().parents[1]
        cli = root / "isolated-mineru/cli"
        cli.mkdir(parents=True)
        originals = {
            "agent_capacity_config": service
            / "src/disclosure_anchor/application/contracts/mineru_capacity_config.py",
            "agent_capacity_file": service
            / "src/disclosure_anchor/adapters/runtime/mineru_capacity_file.py",
            "agent_capacity_bootstrap": service
            / "scripts/windows/mineru_heap_trim_compat/agent_capacity_bootstrap.py",
        }
        self.originals = {}
        for name, source in originals.items():
            raw = source.read_bytes()
            target = cli / (name + ".py")
            target.write_bytes(raw)
            if target.read_bytes() != raw:
                raise AssertionError("standalone source copy differs")
            self.originals[name] = (source, target, raw)
        self.prior = {
            name: module
            for name, module in sys.modules.items()
            if name == "mineru" or name.startswith("mineru.")
        }
        for name in self.prior:
            del sys.modules[name]
        try:
            for name, path in (("mineru", cli.parent), ("mineru.cli", cli)):
                package = types.ModuleType(name)
                package.__path__ = [str(path)]
                sys.modules[name] = package
            for name in originals:
                full_name = "mineru.cli." + name
                spec = importlib.util.spec_from_file_location(
                    full_name, cli / (name + ".py")
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[full_name] = module
                spec.loader.exec_module(module)
            self.codec = sys.modules["mineru.cli.agent_capacity_config"]
            self.reader = sys.modules["mineru.cli.agent_capacity_file"]
            self.bootstrap = sys.modules["mineru.cli.agent_capacity_bootstrap"]
        except BaseException:
            self.close()
            raise

    def close(self):
        for name in tuple(sys.modules):
            if name == "mineru" or name.startswith("mineru."):
                del sys.modules[name]
        sys.modules.update(self.prior)
