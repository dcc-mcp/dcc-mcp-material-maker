from dcc_mcp_core.skill import run_main

from dcc_mcp_material_maker.skill_tools import bridge_main

main = bridge_main("export_material", "Material Maker material exported.")

if __name__ == "__main__":
    run_main(main)
