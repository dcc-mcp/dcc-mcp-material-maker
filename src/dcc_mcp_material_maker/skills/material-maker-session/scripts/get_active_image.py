from dcc_mcp_core.skill import run_main, skill_entry, skill_success

from dcc_mcp_material_maker.bridge import get_bridge


@skill_entry
def main(**_kwargs):
    return skill_success(
        "Active MATERIAL_MAKER image inspected.",
        image=get_bridge().call("material_maker.get_active_image"),
    )


if __name__ == "__main__":
    run_main(main)
