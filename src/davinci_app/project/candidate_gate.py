"""成片候选发布的不可绕过门禁；技术渲染成功本身绝不构成候选资格。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


REQUIRED_CANDIDATE_STAGES = (
    "evidence_bundle",
    "source_understanding",
    "editorial_direction",
    "sound_advice",
    "visual_advice",
    "typography_advice",
    "edit_plan",
    "capability_binding",
    "work_preview_review",
    "finishing_adjustment",
    "candidate_validation",
)


class CandidatePublishGateError(ValueError):
    """候选发布缺少专业前置产物或它们不是同一基线。"""


@dataclass(frozen=True)
class CandidatePublishProof:
    plan_digest: str
    work_preview_id: str
    candidate_render_id: str


def verify_candidate_publishable(
    stage_artifacts: Iterable[dict[str, Any]],
    video_artifacts: Iterable[dict[str, Any]],
    candidate_render_id: str,
) -> CandidatePublishProof:
    """验证专业事实、工作版与最终渲染之间的可追溯关系。"""
    stages = {str(item["artifact_type"]): item for item in stage_artifacts}
    videos = list(video_artifacts)
    errors: list[str] = []

    for artifact_type in REQUIRED_CANDIDATE_STAGES:
        artifact = stages.get(artifact_type)
        if not artifact or artifact.get("state") != "succeeded":
            errors.append(f"缺少已完成的专业产物：{artifact_type}")

    candidate = next((item for item in videos if item.get("id") == candidate_render_id), None)
    if not candidate or candidate.get("artifact_type") != "candidate_render":
        errors.append("候选发布必须引用已验证的 candidate_render，不接受任意 MP4 路径。")
    elif candidate.get("state") != "verified":
        errors.append("候选渲染尚未通过文件级技术验证。")
    work_preview = next((item for item in videos if item.get("artifact_type") == "work_preview"), None)
    if not work_preview or work_preview.get("state") != "verified":
        errors.append("缺少已验证的内部工作版渲染。")

    if errors:
        raise CandidatePublishGateError("；".join(errors))

    edit_plan = _payload(stages["edit_plan"])
    binding = _payload(stages["capability_binding"])
    work_review = _payload(stages["work_preview_review"])
    finishing = _payload(stages["finishing_adjustment"])
    candidate_validation = _payload(stages["candidate_validation"])
    plan_digest = str(edit_plan.get("digest") or "")
    finishing_digest = str(finishing.get("digest") or "")
    assert candidate is not None and work_preview is not None

    if not plan_digest:
        errors.append("EditPlan 缺少稳定摘要。")
    if binding.get("edit_plan_digest") != plan_digest:
        errors.append("CapabilityBinding 与 EditPlan 不是同一基线。")
    if work_preview.get("plan_digest") != plan_digest:
        errors.append("内部工作版不是由当前 EditPlan 生成。")
    if work_review.get("work_preview_hash") != work_preview.get("output_hash"):
        errors.append("工作版复核没有绑定到实际工作版文件。")
    if work_review.get("ready_for_finishing") is not True:
        errors.append("工作版复核尚未明确允许进入收尾。")
    if finishing.get("edit_plan_digest") != plan_digest or finishing.get("work_preview_hash") != work_preview.get("output_hash"):
        errors.append("收尾方案没有绑定到当前 EditPlan 与工作版。")
    if not finishing_digest:
        errors.append("收尾方案缺少稳定摘要。")
    if finishing.get("ready_for_candidate") is not True:
        errors.append("收尾方案尚未明确允许生成候选。")
    if candidate.get("plan_digest") != plan_digest or candidate.get("finishing_digest") != finishing_digest:
        errors.append("候选渲染没有绑定到当前计划与收尾方案。")
    if candidate_validation.get("candidate_render_hash") != candidate.get("output_hash"):
        errors.append("候选验证没有绑定到实际候选渲染文件。")
    if candidate_validation.get("edit_plan_digest") != plan_digest:
        errors.append("候选验证没有绑定到当前 EditPlan。")
    if candidate_validation.get("finishing_digest") != finishing_digest:
        errors.append("候选验证没有绑定到当前收尾方案。")
    if candidate_validation.get("valid") is not True:
        errors.append("候选验证尚未明确通过。")

    if errors:
        raise CandidatePublishGateError("；".join(errors))
    return CandidatePublishProof(
        plan_digest=plan_digest,
        work_preview_id=str(work_preview["id"]),
        candidate_render_id=str(candidate["id"]),
    )


def _payload(artifact: dict[str, Any]) -> dict[str, Any]:
    payload = artifact.get("payload")
    return payload if isinstance(payload, dict) else {}
