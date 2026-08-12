from pathlib import Path

import dcc_mcp_material_maker
from dcc_mcp_material_maker.server import MaterialMakerMcpServer


def test_server_is_standalone_and_bundles_typed_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("DCC_MCP_DISABLE_DEFAULT_SKILL_PATHS", "1")
    server = MaterialMakerMcpServer(port=0, registry_dir=str(tmp_path / "registry"))
    assert server._options.instance_type == "standalone"
    skill_file = (
        Path(dcc_mcp_material_maker.__file__).parent
        / "skills"
        / "material-maker-materials"
        / "SKILL.md"
    )
    assert skill_file.is_file()
