from . import analysis, build, devices, ota, packages, project, quality, system

MODULES = (system, project, build, devices, quality, packages, analysis, ota)


def register_all(mcp) -> None:
    for module in MODULES:
        module.register(mcp)
