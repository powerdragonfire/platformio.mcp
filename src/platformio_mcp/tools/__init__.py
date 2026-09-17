from . import analysis, build, devices, flash, ota, packages, power, project, quality, system

MODULES = (system, project, build, devices, quality, packages, analysis, ota, flash, power)


def register_all(mcp) -> None:
    for module in MODULES:
        module.register(mcp)
