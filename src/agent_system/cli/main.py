"""
CLI 入口 — 命令行调用 Agent 系统
"""

import asyncio
import json
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from agent_system.agents.product_agent import ProductAgent
from agent_system.core.graph import run_agent_sync

console = Console()
app = typer.Typer(help="企业级多 Agent 协作平台")


@app.command()
def run(
    task: str = typer.Argument(..., help="任务描述"),
    agent: str = typer.Option("product", "--agent", "-a", help="使用的 Agent"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="输出文件路径"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON 格式输出"),
):
    """运行单个 Agent 任务"""
    console.print(f"[bold]🚀 Agent System[/bold] — Agent: [cyan]{agent}[/cyan]")
    console.print(f"  任务: {task[:100]}{'...' if len(task) > 100 else ''}")
    console.print()

    if agent == "product":
        agent_instance = ProductAgent()
    else:
        console.print(f"[red]未知 Agent: {agent}[/red]")
        raise typer.Exit(code=1)

    with console.status("[bold green]Agent 执行中..."):
        result = run_agent_sync(agent_instance, task)

    if result["status"] == "failed":
        console.print(Panel(f"[red]执行失败: {result.get('error', '未知错误')}[/red]",
                            title="❌ 失败", border_style="red"))
        raise typer.Exit(code=1)

    output_data = result.get("output", {})

    if json_output:
        console.print(json.dumps(output_data, ensure_ascii=False, indent=2))
    else:
        _display_output(output_data)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        console.print(f"\n[green]✓[/green] 输出已保存到: {output}")


@app.command()
def agents():
    """列出所有可用 Agent"""
    table = Table(title="可用 Agent")
    table.add_column("Agent 名称", style="cyan")
    table.add_column("描述", style="white")
    table.add_column("能力", style="green")

    table.add_row("product", "产品 Agent — 写 PRD", "需求分析, PRD 编写, 功能拆解")

    console.print(table)


@app.command()
def version():
    """显示版本信息"""
    console.print("[bold]Agent System[/bold] v0.1.0")
    console.print("企业级多 Agent 协作平台")
    console.print("基于 ARCHITECTURE.md v15.1")


def _display_output(output: dict):
    """展示输出结果"""
    payload = output.get("payload", {})
    title = payload.get("title", "产出物")
    output_id = output.get("id", "N/A")
    output_type = output.get("type", "N/A")
    created_by = output.get("created_by", "N/A")

    # 头部信息
    meta = Table.grid(padding=(0, 2))
    meta.add_column("字段", style="bold")
    meta.add_column("值")
    meta.add_row("ID", output_id)
    meta.add_row("类型", output_type)
    meta.add_row("创建者", created_by)
    console.print(Panel(meta, title=f"📄 {title}", border_style="blue"))

    # PRD 内容
    if output_type == "requirement":
        _display_prd(payload)


def _display_prd(payload: dict):
    """展示 PRD 内容"""
    if features := payload.get("features"):
        console.print("\n[bold]功能列表:[/bold]")
        for f in features:
            priority = f.get("priority", "P2")
            color = {"P0": "red", "P1": "yellow", "P2": "green"}.get(priority, "white")
            console.print(f"  [{color}][{priority}][/] [bold]{f.get('name')}[/]")
            console.print(f"       {f.get('description', '')}")

    if goals := payload.get("goals"):
        console.print("\n[bold]目标:[/bold]")
        for g in goals:
            console.print(f"  • {g}")

    if constraints := payload.get("constraints"):
        console.print("\n[bold]约束条件:[/bold]")
        for c in constraints:
            console.print(f"  • {c}")

    if timeline := payload.get("timeline"):
        console.print(f"\n[bold]时间估算:[/bold] {timeline}")

    if background := payload.get("background"):
        console.print(f"\n[bold]背景:[/bold] {background[:200]}")


if __name__ == "__main__":
    app()
