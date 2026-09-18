from . import analysis, build, devices, flash, ota, packages, project, quality, system

MODULES = (system, project, build, devices, quality, packages, analysis, ota, flash)


def register_all(mcp) -> None:
    for module in MODULES:
        module.register(mcp)
