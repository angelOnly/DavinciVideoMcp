"""完整专业链路的编排合同；未配置真实分析或 Skill 运行时必须显式阻断。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from davinci_app.common import digest_json
from davinci_app.config import AppConfig
from davinci_app.creative.catalog import CapabilityBindingUnavailable, CreativeCatalog
from davinci_app.media.evidence import EvidenceCompletionError


class ProfessionalPipelineBlocked(RuntimeError):
    """专业前置条件不足；调用方应把运行置为 waiting_user，而不是继续技术剪辑。"""

    def __init__(self, stage: str, reasons: list[str]) -> None:
        self.stage = stage
        self.reasons = reasons
        super().__init__("；".join(reasons))

    def to_detail(self) -> dict[str, Any]:
        return {"code": "professional_prerequisites_missing", "stage": self.stage, "message": str(self), "reasons": self.reasons}


class ProfessionalEvidenceRuntime(Protocol):
    def complete_evidence(
        self, run: dict[str, Any], deterministic_evidence: list[dict[str, Any]], project_paths: dict[str, Path]
    ) -> dict[str, Any]: ...

    def supplement_evidence(
        self,
        run: dict[str, Any],
        complete_evidence: dict[str, Any],
        project_paths: dict[str, Path],
        gaps: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def review_render(
        self, render_path: Path, *, stage: str, context: dict[str, Any]
    ) -> dict[str, Any]: ...


class ProfessionalSkillRuntime(Protocol):
    def invoke(self, skill_name: str, *, mode: str | None, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProfessionalPreproduction:
    edit_plan: dict[str, Any]
    edit_plan_digest: str
    capability_binding: dict[str, Any]


ArtifactRecorder = Callable[[str, dict[str, Any]], None]


class ProfessionalPipeline:
    """按专业职责串联真实外部产物，不内置任何“看起来像剪辑”的伪造结果。"""

    def __init__(
        self,
        evidence_runtime: ProfessionalEvidenceRuntime,
        skill_runtime: ProfessionalSkillRuntime,
        creative_catalog: CreativeCatalog,
    ) -> None:
        self.evidence_runtime = evidence_runtime
        self.skill_runtime = skill_runtime
        self.creative_catalog = creative_catalog

    def build_preproduction(
        self,
        run: dict[str, Any],
        deterministic_evidence: list[dict[str, Any]],
        project_paths: dict[str, Path],
        record: ArtifactRecorder,
    ) -> ProfessionalPreproduction:
        try:
            complete_evidence = self._require_payload(
                self.evidence_runtime.complete_evidence(run, deterministic_evidence, project_paths), "完整媒体证据"
            )
            source = self._invoke(
                "video-source-understanding",
                mode=None,
                payload={"run": run, "evidence": complete_evidence},
            )
            _require_valid_source_understanding(source, complete_evidence)
            gaps = source.get("evidence_gaps")
            if isinstance(gaps, list) and gaps:
                complete_evidence = self._require_payload(
                    self.evidence_runtime.supplement_evidence(run, complete_evidence, project_paths, gaps),
                    "密集补充媒体证据",
                )
                record("evidence_dense_windows", {"request_count": len(gaps), "evidence": complete_evidence})
                source = self._invoke(
                    "video-source-understanding",
                    mode=None,
                    payload={"run": run, "evidence": complete_evidence, "previous_understanding": source},
                )
                _require_valid_source_understanding(source, complete_evidence)
        except EvidenceCompletionError as exc:
            raise ProfessionalPipelineBlocked("complete_evidence", exc.reasons) from exc
        record("evidence_bundle", complete_evidence)
        record("source_understanding", source)
        direction = self._invoke(
            "video-edit-director",
            mode="direction",
            payload={"run": run, "source_understanding": source, "capability_summary": self._capability_summary()},
        )
        record("editorial_direction", direction)
        sound = self._specialist_or_noop("video-sound-rhythm-designer", run, source, direction)
        record("sound_advice", sound)
        visual = self._specialist_or_noop("video-visual-designer", run, source, direction)
        record("visual_advice", visual)
        typography = self._specialist_or_noop("video-typography-designer", run, source, direction)
        record("typography_advice", typography)
        edit_plan = self._invoke(
            "video-edit-director",
            mode="finalize",
            payload={
                "run": run,
                "source_understanding": source,
                "direction": direction,
                "sound_advice": sound,
                "visual_advice": visual,
                "typography_advice": typography,
            },
        )
        execution = edit_plan.get("execution")
        if not isinstance(execution, dict) or not isinstance(execution.get("clips"), list):
            raise ProfessionalPipelineBlocked("finalize_edit_plan", ["专业 EditPlan 缺少可回链 clips，不能进入执行编译。"])
        edit_plan_digest = digest_json(edit_plan)
        record("edit_plan", {"digest": edit_plan_digest, "plan": edit_plan})
        try:
            binding = self.creative_catalog.bind(edit_plan, edit_plan_digest=edit_plan_digest)
        except CapabilityBindingUnavailable as exc:
            raise ProfessionalPipelineBlocked("capability_binding", [str(exc)]) from exc
        record("capability_binding", binding)
        return ProfessionalPreproduction(edit_plan, edit_plan_digest, binding)

    def review_work_preview(
        self,
        run: dict[str, Any],
        preproduction: ProfessionalPreproduction,
        work_preview: dict[str, Any],
        record: ArtifactRecorder,
    ) -> dict[str, Any]:
        try:
            review = self._require_payload(
                self.evidence_runtime.review_render(
                    Path(work_preview["output_path"]),
                    stage="work_preview",
                    context={
                        "run": run,
                        "edit_plan": preproduction.edit_plan,
                        "capability_binding": preproduction.capability_binding,
                    },
                ),
                "内部工作版复核",
            )
        except EvidenceCompletionError as exc:
            raise ProfessionalPipelineBlocked("review_work_preview", exc.reasons) from exc
        review["work_preview_hash"] = work_preview["output_hash"]
        record("work_preview_review", review)
        if review.get("ready_for_finishing") is not True:
            raise ProfessionalPipelineBlocked("review_work_preview", ["内部工作版复核尚未明确允许进入收尾。"])
        finishing = self._invoke(
            "video-finishing-designer",
            mode=None,
            payload={
                "run": run,
                "edit_plan": preproduction.edit_plan,
                "work_preview": work_preview,
                "work_preview_review": review,
            },
        )
        finishing["edit_plan_digest"] = preproduction.edit_plan_digest
        finishing["work_preview_hash"] = work_preview["output_hash"]
        # 摘要覆盖基线引用；后续候选渲染必须使用这份完整收尾方案。
        finishing["digest"] = digest_json(finishing)
        record("finishing_adjustment", finishing)
        if finishing.get("ready_for_candidate") is not True:
            raise ProfessionalPipelineBlocked("finishing_adjustment", ["收尾方案尚未明确允许生成成片候选。"])
        return finishing

    def validate_candidate(
        self,
        candidate_render: dict[str, Any],
        technical_verification: dict[str, Any],
        record: ArtifactRecorder,
    ) -> dict[str, Any]:
        try:
            review = self._require_payload(
                self.evidence_runtime.review_render(
                    Path(candidate_render["output_path"]),
                    stage="candidate",
                    context={
                        "project_id": candidate_render.get("project_id"),
                        "technical_verification": technical_verification,
                    },
                ),
                "候选渲染复核",
            )
        except EvidenceCompletionError as exc:
            raise ProfessionalPipelineBlocked("verify_candidate", exc.reasons) from exc
        result = {
            "candidate_render_hash": candidate_render["output_hash"],
            "edit_plan_digest": candidate_render.get("plan_digest"),
            "finishing_digest": candidate_render.get("finishing_digest"),
            "technical_verification": technical_verification,
            "render_review": review,
            # 只接受明确、可审计的复核结论；默认缺失即不通过。
            "valid": bool(technical_verification.get("valid")) and review.get("ready_for_candidate") is True,
        }
        record("candidate_validation", result)
        if not result["valid"]:
            raise ProfessionalPipelineBlocked("verify_candidate", ["候选渲染尚未通过完整技术与受限专业复核。"])
        return result

    def _capability_summary(self) -> dict[str, int]:
        return {"certified_count": self.creative_catalog.certified_count()}

    def _invoke(self, skill_name: str, *, mode: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        return self._require_payload(self.skill_runtime.invoke(skill_name, mode=mode, payload=payload), skill_name)

    def _specialist_or_noop(
        self,
        skill_name: str,
        run: dict[str, Any],
        source: dict[str, Any],
        direction: dict[str, Any],
    ) -> dict[str, Any]:
        """方向明确没有该专业决定时，记录有理由的 no-op，而不是伪造空建议。"""
        requests = direction.get("specialist_requests")
        requested = False
        if isinstance(requests, list):
            requested = any(
                isinstance(item, dict) and item.get("skill") == skill_name
                for item in requests
            )
        if not requested:
            return {
                "status": "not_activated",
                "skill": skill_name,
                "reason": "导演方向未提出该专业的实际待解决决定；保持最小充分处理。",
                "segments": [],
                "capability_requests": [],
                "unresolved": [],
            }
        return self._invoke(
            skill_name,
            mode=None,
            payload={"run": run, "source_understanding": source, "direction": direction},
        )

    @staticmethod
    def _require_payload(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProfessionalPipelineBlocked("professional_output", [f"{label} 没有返回可审计的结构化产物。"])
        return value


class UnavailableProfessionalEvidenceRuntime:
    """没有真实 FunASR/多模态 Adapter 时的安全默认实现。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def complete_evidence(
        self, run: dict[str, Any], deterministic_evidence: list[dict[str, Any]], project_paths: dict[str, Path]
    ) -> dict[str, Any]:
        raise ProfessionalPipelineBlocked(
            "complete_evidence",
            ["当前 Worker 未注入真实完整证据 Adapter，不能把确定性帧和响度结果伪装成素材理解证据。"],
        )

    def review_render(
        self, render_path: Path, *, stage: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        raise ProfessionalPipelineBlocked(f"review_{stage}", ["尚未配置真实的工作版/候选音视频复核 Adapter。"])

    def supplement_evidence(
        self,
        run: dict[str, Any],
        complete_evidence: dict[str, Any],
        project_paths: dict[str, Path],
        gaps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        raise ProfessionalPipelineBlocked("supplement_evidence", ["尚未配置真实的密集媒体证据 Adapter。"])


class UnavailableProfessionalSkillRuntime:
    """防止在未接通 Codex 专业 Skill 运行时伪造方向、建议或 EditPlan。"""

    def invoke(self, skill_name: str, *, mode: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        mode_label = f"/{mode}" if mode else ""
        raise ProfessionalPipelineBlocked(
            "professional_skill_runtime",
            [f"{skill_name}{mode_label} 尚未接入受控 Skill 运行时，不能生成伪造专业产物。"],
        )


def _require_valid_source_understanding(source: dict[str, Any], evidence: dict[str, Any]) -> None:
    """Skill 的结构化 JSON 仍须回链真实素材和时间范围，避免合格语法掩盖虚构事实。"""
    durations: dict[str, float] = {}
    for asset in evidence.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        try:
            duration = float(asset.get("duration_seconds"))
        except (TypeError, ValueError):
            continue
        if duration > 0 and asset.get("asset_id"):
            durations[str(asset["asset_id"])] = duration

    problems: list[str] = []
    for index, unit in enumerate(source.get("semantic_units") or []):
        if not isinstance(unit, dict):
            problems.append(f"语义单元 {index} 不是对象。")
            continue
        _validate_source_range(unit, durations, f"语义单元 {index}", problems, require_evidence_reference=True)
    for index, gap in enumerate(source.get("evidence_gaps") or []):
        if not isinstance(gap, dict):
            problems.append(f"证据缺口 {index} 不是对象。")
            continue
        _validate_source_range(gap, durations, f"证据缺口 {index}", problems, require_evidence_reference=False)
    for index, relationship in enumerate(source.get("relationships") or []):
        if not isinstance(relationship, dict):
            problems.append(f"素材关系 {index} 不是对象。")
            continue
        for field in ("source_asset_id", "target_asset_id"):
            if str(relationship.get(field) or "") not in durations:
                problems.append(f"素材关系 {index} 引用了未冻结素材：{field}。")
    for index, unknown in enumerate(source.get("unknowns") or []):
        if not isinstance(unknown, dict):
            problems.append(f"未知项 {index} 不是对象。")
            continue
        asset_id = unknown.get("asset_id")
        start = unknown.get("start_seconds")
        end = unknown.get("end_seconds")
        if asset_id is None:
            if start is not None or end is not None:
                problems.append(f"未知项 {index} 没有素材 ID 时不能填写时间范围。")
        elif str(asset_id) not in durations:
            problems.append(f"未知项 {index} 引用了未冻结素材。")
        elif start is not None or end is not None:
            _validate_source_range(unknown, durations, f"未知项 {index}", problems, require_evidence_reference=False)
    if problems:
        raise ProfessionalPipelineBlocked("source_understanding", problems)


def _validate_source_range(
    value: dict[str, Any],
    durations: dict[str, float],
    label: str,
    problems: list[str],
    *,
    require_evidence_reference: bool,
) -> None:
    asset_id = str(value.get("asset_id") or "")
    duration = durations.get(asset_id)
    if duration is None:
        problems.append(f"{label} 引用了未冻结素材。")
        return
    try:
        start = float(value.get("start_seconds"))
        end = float(value.get("end_seconds"))
    except (TypeError, ValueError):
        problems.append(f"{label} 缺少有效时间范围。")
        return
    if start < 0 or end <= start or end > duration + 0.01:
        problems.append(f"{label} 时间范围超出素材边界或为空。")
    if require_evidence_reference:
        references = value.get("evidence_references")
        if not isinstance(references, list) or not any(str(item).strip() for item in references):
            problems.append(f"{label} 缺少可回链的证据引用。")
