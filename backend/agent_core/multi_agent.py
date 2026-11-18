"""多 Agent 编排器

子 Agent 架构：
- SimpleAgent      → 纯 ReAct（简单任务）
- PlannerExecutorAgent → Plan-Execute（复杂任务）
- SubagentPool     → 并行多域任务

编排策略由 TaskClassifier（diagnosis.py 中的 L1）决定。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from llm.base import LLMClient, LLMResponse, Message
from utils.logger import get_logger

from .diagnosis import (
    DeepInvestigator,
    DiagnosisAgent,
    DiagnosisConfidence,
    DiagnosisResult,
    TaskClassifier,
    TaskPlan,
    TaskType,
)

logger = get_logger("agent_core.multi_agent")


# ---------------------------------------------------------------------------
# 子 Agent 结果
# ---------------------------------------------------------------------------

@dataclass
class SubAgentResult:
    agent_type: str
    success: bool
    output: Any = None
    error: Optional[str] = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# 多域并行调度器
# ---------------------------------------------------------------------------

class SubagentPool:
    """子 Agent 池：并行执行多个专注于单一领域的子 Agent"""

    def __init__(
        self,
        llm: LLMClient,
        max_concurrency: int = 4,
        max_depth: int = 2,
    ) -> None:
        self.llm = llm
        self.max_concurrency = max_concurrency
        self.max_depth = max_depth

    async def run_parallel(
        self,
        tasks: List[Dict[str, str]],  # [{"domain": "log", "query": "..."}]
        progress_callback: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    ) -> List[SubAgentResult]:
        """
        并行执行多个子任务。
        domain: log | network | process | disk | memory | system
        """
        domain_prompts = {
            "log": (
                "你是日志分析专家。分析以下日志，识别错误模式、异常和潜在根因。\n"
                "输出：{{'errors': [...], 'warnings': [...], 'summary': '简短总结'}}\n\n{query}"
            ),
            "network": (
                "你是网络诊断专家。分析网络连接和状态数据，识别问题。\n"
                "输出：{{'issues': [...], 'connections': [...], 'summary': '简短总结'}}\n\n{query}"
            ),
            "process": (
                "你是进程分析专家。分析进程列表和状态，识别异常进程。\n"
                "输出：{{'suspicious': [...], 'high_cpu_mem': [...], 'summary': '简短总结'}}\n\n{query}"
            ),
            "disk": (
                "你是磁盘 I/O 专家。分析磁盘使用情况，识别异常。\n"
                "输出：{{'high_io': [...], 'large_files': [...], 'summary': '简短总结'}}\n\n{query}"
            ),
            "memory": (
                "你是内存分析专家。分析内存和 swap 使用，识别泄漏。\n"
                "输出：{{'leak_suspect': [...], 'high_mem': [...], 'summary': '简短总结'}}\n\n{query}"
            ),
            "system": (
                "你是系统诊断专家。综合分析系统状态，给出健康报告。\n"
                "输出：{{'status': 'healthy/warning/critical', 'issues': [...], 'summary': '简短总结'}}\n\n{query}"
            ),
        }

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def run_one(domain: str, query: str) -> SubAgentResult:
            nonlocal progress_callback
            start = time.time()
            async with semaphore:
                try:
                    prompt_template = domain_prompts.get(domain, domain_prompts["system"])
                    prompt = prompt_template.format(query=query)
                    resp = await asyncio.to_thread(
                        self.llm.chat, [Message(role="user", content=prompt)]
                    )
                    result = SubAgentResult(
                        agent_type=domain,
                        success=True,
                        output=resp.content,
                        duration_ms=int((time.time() - start) * 1000),
                    )
                except Exception as e:
                    result = SubAgentResult(
                        agent_type=domain,
                        success=False,
                        error=str(e),
                        duration_ms=int((time.time() - start) * 1000),
                    )

                if progress_callback:
                    await progress_callback(domain, result)

                return result

        # 并行启动所有子 Agent
        coros = [run_one(t["domain"], t["query"]) for t in tasks]
        results = await asyncio.gather(*coros, return_exceptions=True)

        return [
            r if isinstance(r, SubAgentResult) else SubAgentResult(
                agent_type=tasks[i]["domain"],
                success=False,
                error=str(r),
            )
            for i, r in enumerate(results)
        ]

    def synthesize_parallel_results(
        self,
        results: List[SubAgentResult],
        original_task: str,
    ) -> str:
        """综合并行子 Agent 结果，生成最终回答"""
        successful = [r for r in results if r.success]
        if not successful:
            return "抱歉，各维度的分析均失败了，请稍后重试。"

        summary_lines = ["## 多维度分析报告\n", f"**任务：** {original_task}\n\n"]
        for r in results:
            status = "✅" if r.success else "❌"
            summary_lines.append(f"### {status} {r.agent_type} 分析")
            if r.success:
                content = r.output[:400] if isinstance(r.output, str) else str(r.output)
                summary_lines.append(f"{content}\n")
            else:
                summary_lines.append(f"分析失败：{r.error}\n")

        # 用 LLM 综合
        try:
            synthesis_prompt = (
                f"原始任务：{original_task}\n\n"
                + "\n\n".join(
                    f"[{r.agent_type}]: {r.output[:300]}" for r in successful
                )
                + "\n\n请综合以上各维度分析结果，给出最终回答和建议。"
            )
            resp = self.llm.chat([Message(role="user", content=synthesis_prompt)])
            summary_lines.append(f"\n## 综合结论\n{resp.content}")
        except Exception as e:
            logger.warning(f"synthesis LLM call failed: {e}")

        return "\n".join(summary_lines)


# ---------------------------------------------------------------------------
# 规划-执行 Agent（复杂任务）
# ---------------------------------------------------------------------------

@dataclass
class ExecutionStep:
    """执行计划中的单步"""
    step_id: int
    description: str
    tool_name: Optional[str] = None
    depends_on: List[int] = field(default_factory=list)
    parallel_with: List[int] = field(default_factory=list)


class PlannerExecutorAgent:
    """规划-执行 Agent（Plan-and-Execute）

    适用于复杂多步任务：
    1. Planner 分析任务，生成执行计划
    2. Executor 按计划执行（支持依赖和并行）
    3. 返回结构化结果
    """

    def __init__(self, llm: LLMClient, max_steps: int = 10) -> None:
        self.llm = llm
        self.max_steps = max_steps

    async def plan(self, task: str) -> List[ExecutionStep]:
        """Planner：生成执行计划"""
        prompt = f"""你是运维任务规划专家。将以下任务拆解为有序的执行步骤。

要求：
- 每步只完成一个原子操作
- 明确步骤间的依赖关系（depends_on）
- 优先并行无依赖的步骤（parallel_with）
- 只使用以下工具：process.list, process.kill, network.list, network.connectivity,
  disk.usage, disk.find_large, disk.cleanup, log.journal, file.read, file.remove,
  service.status, service.restart, shell.execute, system.lsof, system.memory

任务：{task}

请以 JSON 格式输出执行计划：
{{
  "steps": [
    {{
      "step_id": 1,
      "description": "步骤描述",
      "tool_name": "tool.name 或 null（如果无需工具）",
      "depends_on": [],  // 依赖的 step_id 列表
      "parallel_with": []  // 可并行执行的 step_id 列表
    }}
  ]
}}"""
        try:
            from llm.base import Message
            resp = self.llm.chat([Message(role="user", content=prompt)])
            import json, re
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", resp.content, re.DOTALL)
            if not match:
                match = re.search(r"(\{.*\})", resp.content, re.DOTALL)
            if match:
                d = json.loads(match.group(1))
                steps = []
                for s in d.get("steps", []):
                    steps.append(ExecutionStep(
                        step_id=s.get("step_id", 0),
                        description=s.get("description", ""),
                        tool_name=s.get("tool_name"),
                        depends_on=s.get("depends_on", []),
                        parallel_with=s.get("parallel_with", []),
                    ))
                return steps
        except Exception as e:
            logger.warning(f"plan generation failed: {e}")

        # 降级：简单返回单步
        return [ExecutionStep(step_id=1, description=task, tool_name=None)]

    async def execute(
        self,
        steps: List[ExecutionStep],
        executor_func: Callable[[str, Dict], Awaitable[Any]],
        tool_result_handler: Optional[Callable[[str, Any, int], Awaitable[None]]] = None,
    ) -> Dict[int, Any]:
        """
        Executor：按计划执行工具调用。

        Args:
            steps: 执行计划
            executor_func: 执行工具的异步函数 signature(tool_name, args) -> result
            tool_result_handler: 每步执行完的回调 (step_id, result) -> await None

        Returns:
            {step_id: result}
        """
        results: Dict[int, Any] = {}
        completed: set = set()

        # 构建依赖图
        dep_graph: Dict[int, set] = {s.step_id: set(s.depends_on) for s in steps}

        for step in steps:
            if step.step_id in results:
                continue

            # 等待依赖完成
            while dep_graph[step.step_id] - completed:
                await asyncio.sleep(0.1)

            if step.tool_name:
                try:
                    result = await executor_func(step.tool_name, {})
                    results[step.step_id] = result
                    if tool_result_handler:
                        await tool_result_handler(step.step_id, result, 0)
                except Exception as e:
                    results[step.step_id] = {"error": str(e)}
            else:
                results[step.step_id] = {"description": step.description, "skipped": False}

            completed.add(step.step_id)

        return results

    def format_plan(self, steps: List[ExecutionStep]) -> str:
        """将执行计划格式化为文本"""
        lines = ["## 执行计划\n"]
        for s in steps:
            deps = f"（依赖 step {s.depends_on}）" if s.depends_on else ""
            parallel = f"（可与 step {s.parallel_with} 并行）" if s.parallel_with else ""
            tool = f" → `{s.tool_name}`" if s.tool_name else ""
            lines.append(f"{s.step_id}. {s.description}{tool} {deps}{parallel}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 多 Agent 编排器（主入口）
# ---------------------------------------------------------------------------

class MultiAgentOrchestrator:
    """
    多 Agent 编排器：协调 SimpleAgent / PlannerExecutorAgent / SubagentPool

    使用 TaskClassifier 决定使用哪种策略。
    """

    def __init__(
        self,
        llm: LLMClient,
        agent_loop_ref: Any = None,  # 引用现有 AgentLoop，用于 ReAct 回退
        max_concurrency: int = 4,
    ) -> None:
        self.llm = llm
        self.agent_loop_ref = agent_loop_ref  # 用于 simple ops 回退到原 ReAct

        self.task_classifier = TaskClassifier()
        self.planner_executor = PlannerExecutorAgent(llm)
        self.subagent_pool = SubagentPool(llm, max_concurrency=max_concurrency)

        # 诊断 Agent 和深调 Agent（延迟初始化，避免循环导入）
        self._diagnosis_agent: Optional[DiagnosisAgent] = None
        self._deep_investigator: Optional[DeepInvestigator] = None

    @property
    def diagnosis_agent(self) -> DiagnosisAgent:
        if self._diagnosis_agent is None:
            from kb.ops_knowledge import get_default_kb
            self._diagnosis_agent = DiagnosisAgent(
                llm_client=self.llm,
                kb=get_default_kb(),
            )
        return self._diagnosis_agent

    @property
    def deep_investigator(self) -> DeepInvestigator:
        if self._deep_investigator is None:
            self._deep_investigator = DeepInvestigator(self.llm)
        return self._deep_investigator

    async def run(
        self,
        task: str,
        session_id: str,
        stream_callback: Optional[Callable[[Any], Awaitable[None]]] = None,
        context_hints: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        主入口：根据任务类型路由到不同 Agent。
        """
        plan = self.task_classifier.classify(task, context_hints)

        logger.info(f"MultiAgentOrchestrator: task_type={plan.task_type.value}, reasoning={plan.reasoning}")

        if stream_callback:
            await stream_callback({
                "type": "agent_decision",
                "data": {
                    "task_type": plan.task_type.value,
                    "reasoning": plan.reasoning,
                },
            })

        if plan.task_type == TaskType.FAULT_DIAGNOSIS:
            return await self._run_diagnosis(task, stream_callback, context_hints)
        elif plan.task_type == TaskType.MULTI_DOMAIN:
            return await self._run_multi_domain(task, plan, stream_callback)
        elif plan.task_type == TaskType.INFO_QUERY:
            # 信息查询 → 走简化 ReAct
            return await self._run_simple_ops(task, session_id, stream_callback)
        else:
            # SIMPLE_OPS → 回退到原 AgentLoop ReAct
            return await self._run_simple_ops(task, session_id, stream_callback)

    async def _run_diagnosis(
        self,
        symptom: str,
        stream_callback: Optional[Callable[[Any], Awaitable[None]]],
        context_hints: Optional[Dict[str, Any]],
    ) -> str:
        """运行 L2 诊断Agent"""
        # L2 诊断
        if stream_callback:
            await stream_callback({"type": "phase", "data": {"phase": "L2_diagnosis"}})

        diag_result = await self.diagnosis_agent.diagnose(
            symptom=symptom,
            context_hints=context_hints,
        )

        if stream_callback:
            await stream_callback({
                "type": "diagnosis_result",
                "data": {
                    "confidence": diag_result.confidence.value,
                    "causes": diag_result.possible_causes,
                    "reasoning_chain": diag_result.reasoning_chain,
                },
            })

        # 置信度高 → 直接返回诊断报告
        if diag_result.confidence != DiagnosisConfidence.LOW:
            return self.diagnosis_agent.format_report(diag_result)

        # 置信度低 → 触发 L3 深调
        if stream_callback:
            await stream_callback({"type": "phase", "data": {"phase": "L3_deep_investigation"}})

        subagent_results = await self.deep_investigator.investigate(
            symptom=symptom,
            suggested_tools=diag_result.suggested_tools,
            context_hints=context_hints,
        )

        if stream_callback:
            for r in subagent_results:
                await stream_callback({
                    "type": "subagent_result",
                    "data": {
                        "role": r.role.value,
                        "success": r.success,
                        "output": r.output if r.success else None,
                        "error": r.error if not r.success else None,
                    },
                })

        return await asyncio.to_thread(
            self.deep_investigator.synthesize, subagent_results, symptom
        )

    async def _run_multi_domain(
        self,
        task: str,
        plan: TaskPlan,
        stream_callback: Optional[Callable[[Any], Awaitable[None]]],
    ) -> str:
        """运行多域并行 Agent"""
        if stream_callback:
            await stream_callback({"type": "phase", "data": {"phase": "parallel_dispatch"}})

        # 构建子任务列表
        domain_map = {
            "process": "process",
            "network": "network",
            "disk": "disk",
            "memory": "memory",
            "log": "log",
            "service": "system",
        }
        sub_tasks = []
        for group in (plan.parallel_groups or []):
            for tool in group:
                domain = "system"
                for d, prefix in domain_map.items():
                    if tool.startswith(prefix):
                        domain = d
                        break
                sub_tasks.append({"domain": domain, "query": f"任务：{task}，使用工具：{tool}"})

        results = await self.subagent_pool.run_parallel(sub_tasks)

        if stream_callback:
            for r in results:
                await stream_callback({
                    "type": "subagent_result",
                    "data": {
                        "agent_type": r.agent_type,
                        "success": r.success,
                        "output": r.output if r.success else None,
                    },
                })

        # synthesize 内部还会调一次同步 LLM，整体丢线程池避免阻塞事件循环
        return await asyncio.to_thread(
            self.subagent_pool.synthesize_parallel_results, results, task
        )

    async def _run_simple_ops(
        self,
        task: str,
        session_id: str,
        stream_callback: Optional[Callable[[Any], Awaitable[None]]],
    ) -> str:
        """回退到原有 AgentLoop ReAct（直接迭代 stream，修复类型不匹配）"""
        if self.agent_loop_ref is None:
            return "无法处理此任务：AgentLoop 引用未设置"

        try:
            async for event in self.agent_loop_ref.stream_run(
                session_id=session_id,
                user_input=task,
                stream_callback=stream_callback,
                # 嵌套调用：跳过防重入闸门 + 不重发 phase（外层已发）
                bypass_reentrancy=True,
                silent_phase=True,
            ):
                if event.type == "final" or event.type == "error":
                    return event.data.get("content", "")
            return ""
        except Exception as e:
            logger.exception("simple_ops fallback failed")
            return f"处理失败：{e}"
