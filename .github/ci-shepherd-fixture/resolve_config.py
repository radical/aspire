from __future__ import annotations

import json
from pathlib import Path


configuration = json.loads(
    Path(__file__).with_name("config.json").read_text(encoding="utf-8")
)
print(configuration["output_directory"])
