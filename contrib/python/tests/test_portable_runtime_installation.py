"""Runtime helpers install the contributed unified distribution from real paths."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from onec_mcp_runtime import macos


@pytest.mark.parametrize('selected', ['repository', 'toolkit'])
@pytest.mark.parametrize('server_id', ['onec83-odata', 'onecLive'])
def test_install_unified_toolkit_from_repository_or_toolkit(tmp_path, monkeypatch, selected, server_id):
    toolkit = tmp_path/'contrib/python'
    toolkit.mkdir(parents=True)
    (toolkit/'pyproject.toml').write_text('[project]\nname="onec-community-toolkit"\n')
    observed=[]
    def run(args, **kwargs):
        observed.append(args)
        assert Path(args[-1], 'pyproject.toml').is_file()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(macos.subprocess,'run',run)
    macos._install_packages({'command':'synthetic-python','repo_root':str(tmp_path if selected=='repository' else toolkit),'server_id':server_id})
    assert len(observed)==1
    assert observed[0][-2:] == ['-e',str(toolkit)]


def test_bridge_descriptor_uses_existing_contributed_source_path(tmp_path):
    toolkit=tmp_path/'contrib/python'
    source=toolkit/'src/agent_bridge'
    source.mkdir(parents=True)
    (toolkit/'pyproject.toml').write_text('[project]\nname="onec-community-toolkit"\n')
    document={'mcp_servers':{'onecLive':{'command':'python','args':['-m','agent_bridge'],'env':{'PYTHONPATH':'/synthetic/other'}}}}
    descriptor=macos._extract_stdio_descriptor(document,'onecLive',tmp_path)
    assert Path(descriptor['environment']['PYTHONPATH'].split(':')[0])==source
