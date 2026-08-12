from dcc_mcp_core.skill import run_main

from dcc_mcp_material_maker.skill_tools import bridge_main

main = bridge_main("validate_project", "Material Maker project validated.")

if __name__ == "__main__":
    run_main(main)
