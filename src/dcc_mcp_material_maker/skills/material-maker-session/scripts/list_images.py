from dcc_mcp_core.skill import run_main, skill_entry, skill_success

from dcc_mcp_material_maker.bridge import get_bridge


@skill_entry
def main(**_kwargs):
    images = get_bridge().call("material_maker.list_images")
    return skill_success(f"Found {len(images)} open image(s).", count=len(images), images=images)


if __name__ == "__main__":
    run_main(main)
