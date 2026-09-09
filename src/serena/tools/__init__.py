# ruff: noqa
from .tools_base import *
from .file_tools import *
from .symbol_tools import *
from .memory_tools import *
from .cmd_tools import *
from .job_tools import *
from .output_tools import *
from .media_tools import *
from .git_tools import *
from .config_tools import *
from .workflow_tools import *


# The standalone Serena product exposes one explicit ChatGPT tool catalogue.
# OpenDashboardTool is registered here but is exposed only when dashboard runtime settings make it useful.
MCP_TOOL_CLASSES = (
    CreateTextFileTool,
    ReplaceContentTool,
    ReplaceInFilesTool,
    ReplaceSymbolBodyTool,
    InsertAfterSymbolTool,
    InsertBeforeSymbolTool,
    ReadFileTool,
    ListDirTool,
    FindFileTool,
    SearchForPatternTool,
    GetSymbolsOverviewTool,
    FindSymbolTool,
    FindReferencingSymbolsTool,
    FindImplementationsTool,
    FindDeclarationTool,
    GetDiagnosticsForFileTool,
    RenameSymbolTool,
    SafeDeleteSymbol,
    WriteMemoryTool,
    ReadMemoryTool,
    ListMemoriesTool,
    DeleteMemoryTool,
    RenameMemoryTool,
    EditMemoryTool,
    ExecuteShellCommandTool,
    StartJobTool,
    JobStatusTool,
    CancelJobTool,
    ReadToolOutputTool,
    FetchMediaFileTool,
    RenderPdfPageTool,
    DownloadFileTool,
    UploadFileTool,
    GitStatusTool,
    GitFetchTool,
    GitLogTool,
    GitDiffTool,
    GitBranchTool,
    GitCommitTool,
    GitPullTool,
    GitPushTool,
    OpenDashboardTool,
    ActivateProjectTool,
    GetCurrentConfigTool,
    OnboardingTool,
    InitialInstructionsTool,
)
