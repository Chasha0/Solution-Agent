"""Stage handlers — 5+1 state machine, one handler per stage."""
from agent.storage import Stage

from .base import BaseStage, StageHandler, StageResult
from .clarify import ClarifyHandler
from .design import DesignHandler
from .finalize import FinalizeHandler
from .react import run_react
from .research import ResearchHandler
from .summarize import SummarizeHandler

STAGE_HANDLERS: dict[Stage, StageHandler] = {
    Stage.clarify: ClarifyHandler(),
    Stage.research: ResearchHandler(),
    Stage.summarize: SummarizeHandler(),
    Stage.design: DesignHandler(),
    Stage.finalize: FinalizeHandler(),
}

__all__ = [
    "STAGE_HANDLERS",
    "StageHandler",
    "StageResult",
    "BaseStage",
    "run_react",
    "ClarifyHandler",
    "ResearchHandler",
    "SummarizeHandler",
    "DesignHandler",
    "FinalizeHandler",
]
