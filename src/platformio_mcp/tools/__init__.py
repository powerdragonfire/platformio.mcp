from . import analysis, build, devices, packages, project, quality, system

MODULES = (system, project, build, devices, quality, packages, analysis)


def register_all(mcp) -> None:
    for module in MODULES:
        module.register(mcp)
