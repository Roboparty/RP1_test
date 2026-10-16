"""Keep legacy gateway tests isolated after enabling the workstation control profile."""
from pathlib import Path

import pytest
import yaml

from factory_hmi.plc import config as plc_config


@pytest.fixture(autouse=True)
def offline_default_plc(monkeypatch, tmp_path: Path):
    template = Path(__file__).resolve().parents[1] / "factory_hmi/config/plc_cabinet.yaml"
    raw = yaml.safe_load(template.read_text())
    raw.update(driver="mock", access_mode="control")
    path = tmp_path / "isolated-plc.yaml"
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(plc_config, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setenv("RP1_FACTORY_PLC_CONFIG", str(path))
