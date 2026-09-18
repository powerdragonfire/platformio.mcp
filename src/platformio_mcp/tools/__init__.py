from . import analysis, build, debug, deps, devices, flash, ota, packages, power, project, quality, runtime, system

MODULES = (system, project, build, devices, quality, packages, analysis, ota, flash, power, deps, runtime, debug)


def register_all(mcp) -> None:
    for module in MODULES:
        module.register(mcp)
