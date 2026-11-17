"""根因诊断引擎（Diagnosis Agent）

三级诊断架构：
- L1：任务分类（TaskClassifier）→ 判断走哪条路径
- L2：知识库检索 + LLM 推理 → 生成诊断报告
- L3：并行子 Agent 深调（当 L2 置信度低时触发）

集成 OpsKnowledgeBase RAG 知识库，
诊断结果注入 LLM 上下文，供后续 ReAct 推理使用。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger("agent_core.diagnosis")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

class TaskType(str, Enum):
    """任务类型枚举"""
    SIMPLE_OPS = "simple_ops"          # 简单查询，直接 ReAct
    FAULT_DIAGNOSIS = "fault_diagnosis"  # 故障诊断，走 L2/L3
    MULTI_DOMAIN = "multi_domain"      # 多域任务，并行子 Agent
    INFO_QUERY = "info_query"          # 只读信息查询


class DiagnosisConfidence(str, Enum):
    HIGH = "high"      # > 0.8
    MEDIUM = "medium"  # 0.5 ~ 0.8
    LOW = "low"        # < 0.5


@dataclass
class DiagnosisResult:
    """L2 诊断结果"""
    symptom: str
    possible_causes: List[Dict[str, Any]]  # [{cause, likelihood, reasoning}, ...]
    confidence: DiagnosisConfidence
    suggested_tools: List[str]
    kb_scenario_ids: List[str]
    reasoning_chain: str
    needs_escalation: bool = False  # 是否需要 L3 深调


@dataclass
class TaskPlan:
    """L1 任务规划结果"""
    task_type: TaskType
    reasoning: str
    suggested_steps: List[str]
    escalate_to_diagnosis: bool = False
    parallel_groups: Optional[List[List[str]]] = None


# ---------------------------------------------------------------------------
# L1：任务分类器
# ---------------------------------------------------------------------------

class TaskClassifier:
    """L1 分类器：分析用户输入，决定执行路径"""

    # 强烈暗示故障诊断的模式
    DIAGNOSIS_PATTERNS = [
        r"为什么.*(不行|失败|错误|异常|不能|无法)",
        r"(找不到|连接不上|启动不了|无法启动)",
        r"排查|诊断|根因|原因分析",
        r"(cpu|内存|磁盘|网络).*占用.*高",
        r"(cpu|内存|磁盘|网络).*(100%|满了|占满|爆了)",
        r"服务.*(挂了|崩溃|重启|停止|down)",
        r"(系统|服务).*不正常",
        r".*(error|failed|exception|critical).*",
        r"怎么(回事|办|处理|解决)",
        r"(log|日志).*(error|failed|exception)",
        r"(进程|进程).*(堆集|僵死|僵尸)",
    ]

    # 暗示多域并行的模式
    MULTI_DOMAIN_PATTERNS = [
        r"同时.*(检查|查看|分析)",
        r"(进程|网络|磁盘|内存).*和.*(进程|网络|磁盘|内存)",
        r"全面检查|整体诊断|系统体检",
    ]

    # 暗示只读查询的模式
    INFO_PATTERNS = [
        r"怎么(看|查|找|显示|获取)",
        r"帮我(看|查|找|显示|获取)",
        r"列出|展示|查看.*状态",
        r"当前.*(是|在|有)",
    ]

    def classify(self, user_input: str, context_hints: Optional[Dict[str, Any]] = None) -> TaskPlan:
        """分析用户输入，返回任务类型和执行计划"""
        text = user_input.lower().strip()

        # 1. 检查故障诊断
        for pat in self.DIAGNOSIS_PATTERNS:
            if re.search(pat, text):
                return TaskPlan(
                    task_type=TaskType.FAULT_DIAGNOSIS,
                    reasoning=f"匹配诊断模式: {pat}",
                    suggested_steps=["diagnosis"],
                    escalate_to_diagnosis=True,
                )

        # 2. 检查多域并行
        for pat in self.MULTI_DOMAIN_PATTERNS:
            if re.search(pat, text):
                return TaskPlan(
                    task_type=TaskType.MULTI_DOMAIN,
                    reasoning=f"多域任务: {pat}",
                    suggested_steps=["parallel_dispatch"],
                    parallel_groups=[
                        ["process.list", "network.list", "disk.usage", "system.memory"],
                        ["log.journal"],
                    ],
                )

        # 3. 检查只读查询
        for pat in self.INFO_PATTERNS:
            if re.search(pat, text):
                return TaskPlan(
                    task_type=TaskType.INFO_QUERY,
                    reasoning=f"信息查询: {pat}",
                    suggested_steps=["react"],
                )

        # 4. 默认走 ReAct
        return TaskPlan(
            task_type=TaskType.SIMPLE_OPS,
            reasoning="默认 ReAct 路径",
            suggested_steps=["react"],
        )


# ---------------------------------------------------------------------------
# L2：诊断 Agent
# ---------------------------------------------------------------------------

class DiagnosisAgent:
    """L2 诊断 Agent：结合 RAG 知识库 + LLM 推理"""

    def __init__(
        self,
        llm_client: Any,
        kb: Any = None,
        min_confidence: float = 0.5,
    ) -> None:
        self.llm = llm_client
        self.kb = kb
        self.min_confidence = min_confidence

    async def diagnose(
        self,
        symptom: str,
        context_hints: Optional[Dict[str, Any]] = None,
    ) -> DiagnosisResult:
        """
        诊断流程：
        1. 从知识库检索相关故障场景（BM25）
        2. LLM 结合场景做结构化推理
        3. 输出根因报告
        """
        # Step 1: RAG 检索
        kb_matches: List[Dict[str, Any]] = []
        if self.kb:
            try:
                kb_matches = self.kb.retrieve(symptom, top_k=5)
            except Exception as e:
                logger.warning(f"KB retrieval failed: {e}")

        # Step 2: LLM 结构化推理
        prompt = self._build_diagnosis_prompt(symptom, kb_matches, context_hints)

        try:
            from llm.base import Message
            resp = await asyncio.to_thread(
                self.llm.chat, [Message(role="user", content=prompt)]
            )
            raw_content = resp.content
        except Exception as e:
            logger.error(f"LLM diagnosis failed: {e}")
            raw_content = ""

        # Step 3: 解析 LLM 输出
        result = self._parse_response(raw_content, symptom, kb_matches)

        # Step 4: 判断是否需要 L3 深调
        if result.confidence == DiagnosisConfidence.LOW and not result.possible_causes:
            result.needs_escalation = True

        logger.info(
            f"Diagnosis: symptom={symptom[:30]}, "
            f"confidence={result.confidence.value}, "
            f"causes={len(result.possible_causes)}, "
            f"escalation={result.needs_escalation}"
        )
        return result

    def _build_diagnosis_prompt(
        self,
        symptom: str,
        kb_matches: List[Dict[str, Any]],
        context_hints: Optional[Dict[str, Any]],
    ) -> str:
        """构建诊断 prompt"""
        # 构造知识库场景文本
        scenario_lines = []
        if kb_matches:
            for m in kb_matches:
                s = m.get("scenario", {})
                scenario_lines.append(
                    f"## 场景：{s.get('scenario', '未知')}\n"
                    f"症状：{'；'.join(s.get('symptoms', []))}\n"
                    f"根因：{s.get('root_cause', '未知')}\n"
                    f"解决方案：{'；'.join(s.get('solutions', []))}\n"
                )
        kb_text = "\n\n".join(scenario_lines) if scenario_lines else "（知识库无匹配场景）"

        # 构造上下文提示
        ctx_lines = []
        if context_hints:
            for k, v in context_hints.items():
                if v:
                    ctx_lines.append(f"- {k}: {str(v)[:200]}")
        ctx_text = "\n".join(ctx_lines) if ctx_lines else "无额外上下文"

        return f"""你是资深 Linux 运维工程师，擅长根因分析。

## 当前故障描述
{symptom}

## 当前系统上下文（部分感知数据）
{ctx_text}

## 相关故障知识库案例
{kb_text}

## 推理要求
请基于上述信息，按以下步骤推理：
1. 识别最可能的 2~3 个根因（按可能性排序）
2. 为每个根因给出支撑理由
3. 推荐应使用的工具（如 lsof、journalctl、iotop 等）
4. 评估诊断置信度（高/中/低）

## 输出格式
请以以下 JSON 格式输出（只输出 JSON，不要有其他文字）：
{{
  "symptom": "当前故障描述",
  "possible_causes": [
    {{"cause": "根因描述", "likelihood": 0.85, "reasoning": "推理理由"}},
    {{"cause": "根因描述", "likelihood": 0.60, "reasoning": "推理理由"}}
  ],
  "confidence": "high",
  "suggested_tools": ["tool1", "tool2"],
  "reasoning_chain": "## 推理链\\nStep 1: ...\\nStep 2: ..."
}}"""

    def _parse_response(
        self,
        content: str,
        symptom: str,
        kb_matches: List[Dict[str, Any]],
    ) -> DiagnosisResult:
        """解析 LLM 输出为 DiagnosisResult"""
        import json, re

        # 尝试从 markdown 代码块中提取 JSON
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if match:
            json_str = match.group(1)
        else:
            # 尝试直接匹配 JSON 对象
            match = re.search(r"(\{[^}]+\{.*?\}[^}]*\})", content, re.DOTALL)
            if match:
                json_str = match.group(1)
            else:
                json_str = ""

        if json_str:
            try:
                d = json.loads(json_str)
                causes = []
                for c in d.get("possible_causes", []):
                    if isinstance(c, dict):
                        causes.append(c)
                    elif isinstance(c, str):
                        causes.append({"cause": c, "likelihood": 0.5, "reasoning": ""})

                confidence_str = d.get("confidence", "low").lower()
                if confidence_str in ("high", "高"):
                    confidence = DiagnosisConfidence.HIGH
                elif confidence_str in ("medium", "中"):
                    confidence = DiagnosisConfidence.MEDIUM
                else:
                    confidence = DiagnosisConfidence.LOW

                return DiagnosisResult(
                    symptom=d.get("symptom", symptom),
                    possible_causes=causes,
                    confidence=confidence,
                    suggested_tools=d.get("suggested_tools", []),
                    kb_scenario_ids=[m.get("scenario", {}).get("id", "") for m in kb_matches],
                    reasoning_chain=d.get("reasoning_chain", content[:300]),
                    needs_escalation=(confidence == DiagnosisConfidence.LOW),
                )
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"JSON parse failed: {e}")

        # 降级：基于 KB 匹配生成简单报告
        if kb_matches:
            s = kb_matches[0].get("scenario", {})
            return DiagnosisResult(
                symptom=symptom,
                possible_causes=[{
                    "cause": s.get("root_cause", "未知根因"),
                    "likelihood": 0.6,
                    "reasoning": "基于知识库场景匹配",
                }],
                confidence=DiagnosisConfidence.MEDIUM,
                suggested_tools=s.get("tools_used", []),
                kb_scenario_ids=[s.get("id", "")],
                reasoning_chain=f"知识库匹配场景：{s.get('scenario', '未知')}",
                needs_escalation=True,
            )

        # 完全降级
        return DiagnosisResult(
            symptom=symptom,
            possible_causes=[{
                "cause": "无法确定根因，需进一步调查",
                "likelihood": 0.0,
                "reasoning": "知识库无匹配，LLM 解析失败",
            }],
            confidence=DiagnosisConfidence.LOW,
            suggested_tools=["process.list", "network.list", "disk.usage", "log.journal"],
            kb_scenario_ids=[],
            reasoning_chain="诊断失败，建议触发 L3 深调",
            needs_escalation=True,
        )

    def format_report(self, result: DiagnosisResult) -> str:
        """将诊断结果格式化为可读文本"""
        lines = [
            f"## 根因诊断报告\n",
            f"**故障描述：** {result.symptom}",
            f"**置信度：** {result.confidence.value.upper()}",
            "",
            "**可能根因（按可能性排序）：**",
        ]
        for i, c in enumerate(result.possible_causes, 1):
            likelihood = c.get("likelihood", 0.0)
            lines.append(f"{i}. {c.get('cause', '未知')} (置信度: {likelihood:.0%})")
            if c.get("reasoning"):
                lines.append(f"   推理：{c.get('reasoning')}")

        if result.suggested_tools:
            lines.append(f"\n**推荐工具：** {', '.join(result.suggested_tools)}")

        lines.append(f"\n**推理链：**\n{result.reasoning_chain}")

        if result.needs_escalation:
            lines.append("\n⚠️ L2 置信度低，建议触发 L3 深调")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# L3：深调 Agent（并行子 Agent）
# ---------------------------------------------------------------------------

class SubagentRole(str, Enum):
    LOG_ANALYSIS = "log_analysis"
    NETWORK_ANALYSIS = "network_analysis"
    PROCESS_ANALYSIS = "process_analysis"
    DISK_ANALYSIS = "disk_analysis"
    MEMORY_ANALYSIS = "memory_analysis"
    DIAGNOSIS = "diagnosis"


@dataclass
class SubagentTask:
    role: SubagentRole
    query: str
    context: Dict[str, Any] = field(default_factory=dict)
    depth: int = 0


@dataclass
class SubagentResult:
    role: SubagentRole
    success: bool
    output: Any = None
    error: Optional[str] = None
    duration_ms: int = 0


_ROLE_PROMPTS: Dict[SubagentRole, str] = {
    SubagentRole.LOG_ANALYSIS: (
        "你是一个日志分析专家。分析以下日志数据，识别错误模式、异常和潜在根因。"
        "输出格式：{{'errors': [...], 'warnings': [...], 'anomalies': [...], 'summary': '...'}}\n\n"
        "Query: {query}\nContext: {context}"
    ),
    SubagentRole.NETWORK_ANALYSIS: (
        "你是一个网络诊断专家。分析网络连接数据，识别连接问题和异常。"
        "输出格式：{{'issues': [...], 'healthy_connections': [...], 'recommendations': [...]}}\n\n"
        "Query: {query}\nContext: {context}"
    ),
    SubagentRole.PROCESS_ANALYSIS: (
        "你是一个进程分析专家。分析进程数据，识别资源问题、僵尸进程或可疑活动。"
        "输出格式：{{'suspicious': [...], 'high_cpu': [...], 'high_mem': [...], 'summary': '...'}}\n\n"
        "Query: {query}\nContext: {context}"
    ),
    SubagentRole.DISK_ANALYSIS: (
        "你是一个磁盘 I/O 专家。分析磁盘使用和 I/O 模式。"
        "输出格式：{{'high_io': [...], 'large_files': [...], 'recommendations': [...]}}\n\n"
        "Query: {query}\nContext: {context}"
    ),
    SubagentRole.MEMORY_ANALYSIS: (
        "你是一个内存分析专家。分析内存使用数据，识别泄漏和异常。"
        "输出格式：{{'leak_suspect': [...], 'high_mem_processes': [...], 'swap_usage': '...', 'summary': '...'}}\n\n"
        "Query: {query}\nContext: {context}"
    ),
    SubagentRole.DIAGNOSIS: (
        "你是资深运维诊断 Agent。根据以下症状进行深度根因分析。\n\n"
        "Query: {query}\nContext: {context}"
    ),
}


class DeepInvestigator:
    """L3 深调 Agent：并行派发多个子 Agent，从多维度深调"""

    def __init__(
        self,
        llm_client: Any,
        max_concurrency: int = 4,
        max_depth: int = 2,
    ) -> None:
        self.llm = llm_client
        self.max_concurrency = max_concurrency
        self.max_depth = max_depth

    async def investigate(
        self,
        symptom: str,
        suggested_tools: List[str],
        context_hints: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[SubagentResult], Awaitable[None]]] = None,
    ) -> List[SubagentResult]:
        """
        并行派发子 Agent，从多个维度深调。
        根据 suggested_tools 判断需要哪些子 Agent。
        """
        import asyncio, time

        # 确定需要哪些子 Agent
        roles_needed = self._tools_to_roles(suggested_tools)

        # 构建子 Agent 任务
        tasks = [
            SubagentTask(
                role=role,
                query=f"针对症状 '{symptom}' 进行 {role.value} 维度的深度分析",
                context=(context_hints or {}),
            )
            for role in roles_needed
        ]

        if not tasks:
            return []

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def run_one(task: SubagentTask) -> SubagentResult:
            async with semaphore:
                start = time.time()
                try:
                    if task.depth >= self.max_depth:
                        return SubagentResult(
                            role=task.role,
                            success=False,
                            error=f"max recursion depth {self.max_depth} reached",
                            duration_ms=int((time.time() - start) * 1000),
                        )
                    prompt = _ROLE_PROMPTS.get(task.role, "分析: {query}").format(
                        query=task.query,
                        context=task.context,
                    )
                    from llm.base import Message
                    resp = await asyncio.to_thread(
                        self.llm.chat, [Message(role="user", content=prompt)]
                    )
                    result = SubagentResult(
                        role=task.role,
                        success=True,
                        output=resp.content,
                        duration_ms=int((time.time() - start) * 1000),
                    )
                except Exception as e:
                    result = SubagentResult(
                        role=task.role,
                        success=False,
                        error=str(e),
                        duration_ms=int((time.time() - start) * 1000),
                    )

                if progress_callback:
                    await progress_callback(result)
                return result

        # 并行执行所有子 Agent
        results = await asyncio.gather(*[run_one(t) for t in tasks], return_exceptions=True)
        return [r if isinstance(r, SubagentResult) else SubagentResult(
            role=SubagentRole.DIAGNOSIS, success=False, error=str(r)
        ) for r in results]

    def _tools_to_roles(self, tools: List[str]) -> List[SubagentRole]:
        """根据工具列表推断需要的子 Agent 角色"""
        tool_to_role = {
            "log.journal": SubagentRole.LOG_ANALYSIS,
            "log.": SubagentRole.LOG_ANALYSIS,
            "network.": SubagentRole.NETWORK_ANALYSIS,
            "process.": SubagentRole.PROCESS_ANALYSIS,
            "disk.": SubagentRole.DISK_ANALYSIS,
            "system.memory": SubagentRole.MEMORY_ANALYSIS,
            "system.info": SubagentRole.MEMORY_ANALYSIS,
        }
        roles: List[SubagentRole] = []
        seen: set = set()
        for tool in tools:
            for prefix, role in tool_to_role.items():
                if tool.startswith(prefix) and role not in seen:
                    roles.append(role)
                    seen.add(role)
                    break
        if not roles:
            # 默认全开
            roles = list(SubagentRole)
        return roles

    def synthesize(self, results: List[SubagentResult], original_symptom: str) -> str:
        """综合所有子 Agent 结果，生成最终诊断报告"""
        lines = [f"## L3 深度诊断报告\n\n**原始症状：** {original_symptom}\n"]
        lines.append("### 各维度分析结果：\n")

        for r in results:
            status = "✅" if r.success else "❌"
            lines.append(f"#### {status} {r.role.value}")
            if r.success:
                # 截取前 500 字符
                content = r.output[:500] if isinstance(r.output, str) else str(r.output)
                lines.append(f"```\n{content}\n```")
            else:
                lines.append(f"错误: {r.error}")
            lines.append("")

        successful = [r for r in results if r.success]
        if successful:
            # 让 LLM 综合所有结果
            try:
                from llm.base import Message
                synthesis_prompt = (
                    f"原始症状：{original_symptom}\n\n"
                    + "\n\n".join(
                        f"[{r.role.value}]: {r.output[:300]}" for r in successful
                    )
                    + "\n\n请综合以上各维度分析结果，给出一个综合根因结论和解决方案。"
                )
                resp = self.llm.chat([Message(role="user", content=synthesis_prompt)])
                lines.append(f"### 综合结论\n{resp.content}")
            except Exception as e:
                lines.append(f"综合 LLM 调用失败: {e}")

        return "\n".join(lines)
