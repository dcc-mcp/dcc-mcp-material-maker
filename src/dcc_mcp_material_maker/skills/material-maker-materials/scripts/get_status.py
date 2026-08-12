from dcc_mcp_core.skill import run_main

from dcc_mcp_material_maker.skill_tools import bridge_main

main = bridge_main("status", "Material Maker CLI is ready.")

if __name__ == "__main__":
    run_main(main)
