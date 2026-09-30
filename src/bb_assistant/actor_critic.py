"""Bounded DS drafting and GPT source-grounded review, without automatic approval."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from .services import GradingError


class ActorCriticGrader:
    def __init__(self, actor, critic, config=None, *, progress=None, cancelled=None):
        self.actor, self.critic = actor, critic
        self.options = copy.deepcopy(config or {})
        self.max_revisions = self.options.get("max_revisions", 1)
        if (isinstance(self.max_revisions, bool) or not isinstance(self.max_revisions, int)
                or not 0 <= self.max_revisions <= 2):
            raise ValueError("协作评分 max_revisions 必须是 0 至 2 的整数。")
        self.progress = progress or (lambda message: None)
        self.cancelled = cancelled or (lambda: False)
        # Classification can reuse the same DS cache as an independent DS run.
        self.config = actor.config
        self.last_metadata = {}
        self.trace = {}
        self.on_trace = lambda trace: None

    def classify_images(self, images):
        return self.actor.classify_images(images)

    def _check_cancelled(self):
        if self.cancelled():
            raise GradingError("已取消协作评分；未完成 GPT 复核的草稿仅保留为过程记录。")

    @staticmethod
    def _public_result(result):
        return {key: copy.deepcopy(value) for key, value in result.items()
                if key not in {"raw", "provider_metadata"}}

    def _record(self, role, round_number, result, client):
        metadata = copy.deepcopy(result.get("provider_metadata", client.last_metadata))
        if self.trace["events"]:
            initial = self.trace["events"][0]["metadata"]
            for key in ("text_sha256", "image_sha256", "rubric_sha256", "reference_sha256"):
                if key in initial and metadata.get(key) != initial[key]:
                    raise GradingError("协作各阶段的作答或评分规则发生变化，未接受此轮结果；请重新运行。")
        self.trace["events"].append({
            "role": role, "round": round_number,
            "result": self._public_result(result),
            "metadata": metadata,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        self.on_trace(copy.deepcopy(self.trace))

    def grade(self, text, rubric, reference_answer, max_score, *, scoring_policy=None, images=None):
        self.last_metadata = {}
        self.trace = {"status": "incomplete", "revisions": 0, "max_revisions": self.max_revisions,
                      "events": [], "final_decision": "incomplete"}
        options = {"scoring_policy": copy.deepcopy(scoring_policy)}
        if images:
            options["images"] = images
        try:
            self._check_cancelled()
            self.progress("DS 初评：核对原始作答并生成逐题判断。")
            draft = self.actor.grade(text, rubric, reference_answer, max_score, **options)
            self._record("actor", 0, draft, self.actor)
            for round_number in range(self.max_revisions + 1):
                self._check_cancelled()
                self.progress(f"GPT 复核（第 {round_number + 1} 次）：检查原始作答、错题位置和规则依据。")
                critique = self.critic.critique(text, rubric, reference_answer, max_score,
                                                draft=draft, **options)
                self._record("critic", round_number, critique, self.critic)
                self._check_cancelled()
                decision = critique["decision"]
                self.trace["final_decision"] = decision
                if decision == "accept":
                    self.trace["status"] = "needs_human" if draft.get("uncertainties") else "accepted"
                    break
                if decision == "needs_human" or round_number == self.max_revisions:
                    self.trace["status"] = "needs_human"
                    break
                if decision != "revise":
                    raise GradingError("GPT 返回未知复核结论，未接受协作结果。")
                self._check_cancelled()
                self.progress(f"DS 修订（第 {round_number + 1} 轮）：结合原始作答核验 GPT 指出的分歧。")
                draft = self.actor.grade(text, rubric, reference_answer, max_score,
                                         review_context={"draft": draft, "critic": critique}, **options)
                self.trace["revisions"] = round_number + 1
                self._record("actor", round_number + 1, draft, self.actor)
            result = copy.deepcopy(draft)
            warnings = list(result.get("uncertainties") or [])
            if self.trace["status"] != "accepted":
                warning = "DS / GPT 协作仍有分歧或疑点，请对照原始作答人工判断。"
                warnings.append(warning)
                for item in critique.get("issues", []):
                    detail = f"GPT 复核第 {item['question_id']} 题：{item['feedback']}；依据：{item['evidence']}"
                    if detail not in warnings:
                        warnings.append(detail)
                for warning in critique.get("uncertainties", []):
                    if warning not in warnings:
                        warnings.append(warning)
            result["uncertainties"] = warnings
            self.last_metadata = copy.deepcopy(draft.get("provider_metadata", self.actor.last_metadata))
            self.last_metadata.update(workflow="actor_critic", actor="deepseek", critic="gpt",
                                      review_status=self.trace["status"], revisions=self.trace["revisions"])
            result["provider_metadata"] = copy.deepcopy(self.last_metadata)
            result["actor_critic"] = copy.deepcopy(self.trace)
            self.progress("模型复核通过，仍需人工审核。" if self.trace["status"] == "accepted"
                          else "协作轮次已结束，分歧及原始依据已保留，待人工判断。")
            return result
        except Exception:
            self.trace.update(status="incomplete", final_decision="incomplete")
            # The workflow owns error redaction and persistence. No draft is
            # returned as a successfully reviewed result after a failed call.
            raise
