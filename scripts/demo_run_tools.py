"""
This script demonstrates how to use Serena's tools locally, useful
for testing or development. Here the tools will be operation the serena repo itself.
"""

import json
from pathlib import Path
from pprint import pprint

from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.constants import REPO_ROOT
from serena.tools import GetDiagnosticsForFileTool

if __name__ == "__main__":
    serena_config = SerenaConfig.from_config_file()
    serena_config.web_dashboard = False
    project = Path(REPO_ROOT)
    agent = SerenaAgent(project=str(project), serena_config=serena_config)

    diagnostics_in_file_tool = agent.get_tool(GetDiagnosticsForFileTool)

    result = agent.execute_task(
        lambda: diagnostics_in_file_tool.apply(
            # name_path_pattern="SerenaAgent",
            relative_path="test/resources/repos/clojure/test_repo/src/test_app/diagnostics_sample.clj",
            # keep_definition=True,
        )
    )
    pprint(json.loads(result))
    # input("Press Enter to continue...")
