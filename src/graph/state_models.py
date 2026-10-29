from __future__ import annotations

from typing import Any, TypedDict

from src.agents.contracts import ReviewRequest
from src.graph.runtime import WorkflowRuntimeContext
from src.llm import ProviderSnapshot
from src.paper_retrieval import PaperSearchService
from src.paper_retrieval.models import PaperDocument
from src.repositories.sessions.base import SessionRepository


JsonObject = dict[str, Any]


class State(TypedDict, total=False):
    """LangGraph 兼容的共享图状态。"""

    request: ReviewRequest
    search_results: list[PaperDocument]
    # DEPRECATED: 2026-08-22
    # 原因：关键词打分逻辑已下线，状态里不再携带打分明细。
    # 替代方案：无（打分明细没有下游业务读取）。
    # 计划移除：语义过滤稳定运行一段时间后清理。
    # search_scores: list[JsonObject]
    search_summary: JsonObject
    search_artifact_refs: list[JsonObject]
    read_results: list[JsonObject]
    read_summary: JsonObject
    read_artifact_refs: list[JsonObject]
    read_resume_checkpoint: JsonObject
    analysis_report: JsonObject
    analysis_artifact_refs: list[JsonObject]
    writing_outline: JsonObject
    writing_outline_report: JsonObject
    writing_outline_artifact_refs: list[JsonObject]
    writing_sections: list[JsonObject]
    writing_report: JsonObject
    writing_artifact_refs: list[JsonObject]
    diagnostics: JsonObject
    current_step: str
    session_repo: SessionRepository
    session_key: str
    turn_id: str
    runtime_context: WorkflowRuntimeContext
    search_node_service: PaperSearchService
    search_node_llm: ProviderSnapshot | None | str
    read_node_llm: ProviderSnapshot | None | str
    analysis_node_llm: ProviderSnapshot | None | str
    writing_outline_node_llm: ProviderSnapshot | None | str
    writing_node_llm: ProviderSnapshot | None | str
