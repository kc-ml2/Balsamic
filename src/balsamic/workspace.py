"""The workspace the service runs: the core Workspace with Balsamic's guidance and command handlers."""
from optimization_framework.execution.service import Workspace

from balsamic.guidance import CampaignGuidance


def build_workspace(directory, *, max_workers=2, implementation_client=None, stop_grace_seconds=5, gpu_workers=0):
    workspace = Workspace(directory, max_workers=max_workers, stop_grace_seconds=stop_grace_seconds,
                          implementation_client=implementation_client, guidance=CampaignGuidance, gpu_workers=gpu_workers)
    from balsamic.commands import register
    register(workspace)
    return workspace
