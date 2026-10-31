"""Control API routes for the fault engine: see the state, switch rules on and off."""

from __future__ import annotations

from rigtape.control import Route
from rigtape.faults.engine import FaultEngine


def routes(engine: FaultEngine) -> list[Route]:
    def status(_: dict[str, str]) -> tuple[int, object]:
        return 200, engine.status()

    def toggle(enabled: bool) -> Route:
        def handler(params: dict[str, str]) -> tuple[int, object]:
            name = params["name"]
            if not engine.set_enabled(name, enabled):
                hint = "rules: " + (", ".join(engine.names) or "none")
                return 404, {"error": f"no rule named {name!r}", "hint": hint}
            return 200, {"name": name, "enabled": enabled}

        verb = "enable" if enabled else "disable"
        return "POST", f"/faults/{{name}}/{verb}", handler

    return [("GET", "/faults", status), toggle(True), toggle(False)]
