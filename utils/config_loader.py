"""utils/config_loader.py - 读取 config.yaml"""
import os
import yaml

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
_CONFIG_PATH = os.path.join(project_root, "config.yaml")

_config: dict = {}


def load_config(path: str = _CONFIG_PATH) -> dict:
    global _config
    with open(path, "r", encoding="utf-8") as f:
        _config = yaml.safe_load(f)
    return _config


def get_config() -> dict:
    if not _config:
        load_config()
    return _config
