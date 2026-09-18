from . import analysis, build, deps, devices, flash, ota, packages, power, project, quality, system

MODULES = (system, project, build, devices, quality, packages, analysis, ota, flash, power, deps)


def register_all(mcp) -> None:
    for module in MODULES:
        module.register(mcp)
