"""
Agent System — CLI entry point.

Usage:
  python -m agent_system run "<task>" [--agent <name>] [--json]
  python -m agent_system pipeline "<task>"
  python -m agent_system list
  python -m agent_system graph stats
  python -m agent_system version
"""

import sys
import json as json_mod

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

console = Console()

AGENT_MAP = {
    "product": "agent_system.agents.product_agent.ProductAgent",
    "tech": "agent_system.agents.tech_agent.TechAgent",
    "test": "agent_system.agents.test_agent.TestAgent",
    "devops": "agent_system.agents.devops_agent.DevOpsAgent",
    "security": "agent_system.agents.security_agent.SecurityAgent",
    "docs": "agent_system.agents.docs_agent.DocsAgent",
    "review": "agent_system.agents.review_agent.ReviewAgent",
    "deploy": "agent_system.agents.deploy_agent.DeployAgent",
    "ceo": "agent_system.agents.ceo_agent.CEOAgent",
}

AGENT_DESCRIPTIONS = {
    "product": "Write PRD requirements",
    "tech": "Implement code",
    "test": "Generate and run tests",
    "devops": "Deploy, monitor, manage infrastructure",
    "security": "Security review, CVE scanning, compliance",
    "docs": "Generate API docs, runbooks, ADRs",
    "review": "Peer review code, design, and test plans",
    "deploy": "Manage staging/prod releases and rollbacks",
    "ceo": "Orchestrate full Product→Tech→Test pipeline",
}


def _import_agent(name: str):
    if name not in AGENT_MAP:
        console.print(f"[red]Unknown agent: {name}[/red]")
        console.print(f"Available: {', '.join(AGENT_MAP.keys())}")
        sys.exit(1)
    module_path, class_name = AGENT_MAP[name].rsplit(".", 1)
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


def main():
    if len(sys.argv) < 2:
        console.print("[bold]Agent System v0.1.0[/bold]")
        console.print("Enterprise Multi-Agent Platform (9 agents)")
        console.print()
        console.print("Commands:")
        console.print("  python -m agent_system run <task> [--agent <name>]")
        console.print("  python -m agent_system pipeline <task>")
        console.print("  python -m agent_system list")
        console.print("  python -m agent_system version")
        return

    cmd = sys.argv[1]

    if cmd == "version":
        console.print("[bold]Agent System[/bold] v0.1.0")
        console.print("571 tests · 9 agents · MIT License")
        return

    if cmd == "list":
        table = Table(title="Available Agents (9)")
        table.add_column("Name", style="cyan")
        table.add_column("Class", style="yellow")
        table.add_column("Pipeline", style="white")
        for name, path in AGENT_MAP.items():
            table.add_row(name, path.split(".")[-1], AGENT_DESCRIPTIONS.get(name, ""))
        console.print(table)
        return

    if cmd == "graph":
        from agent_system.cli.graph_cli import graph_app
        if len(sys.argv) > 2:
            graph_app(sys.argv[2:])
        else:
            graph_app(["--help"])
        return

    if cmd == "run":
        if len(sys.argv) < 3:
            console.print("[red]Usage: python -m agent_system run <task> [--agent <name>] [--json][/red]")
            return
        task_input = sys.argv[2]
        agent_name = "product"
        json_output = False
        if "--agent" in sys.argv:
            idx = sys.argv.index("--agent")
            if idx + 1 < len(sys.argv):
                agent_name = sys.argv[idx + 1]
        if "--json" in sys.argv:
            json_output = True

        agent = _import_agent(agent_name)
        from agent_system.core.graph import run_agent_sync

        console.print(f"Agent: [cyan]{agent_name}[/cyan]")
        with console.status("[green]Running..."):
            result = run_agent_sync(agent, task_input)

        if result["status"] == "failed":
            console.print(f"[red]Failed: {result.get('error')}[/red]")
            sys.exit(1)

        output = result.get("output", {})
        if json_output:
            console.print(json_mod.dumps(output, ensure_ascii=False, indent=2))
        else:
            payload = output.get("payload", {})
            title = payload.get("title", output.get("type", "Output"))
            console.print(f"\n[bold]== {title} ==[/bold]")
            console.print(f"  [dim]ID:[/dim] {output.get('id', 'N/A')}")
            console.print(f"  [dim]Type:[/dim] {output.get('type', 'N/A')}")
            if "features" in payload:
                for f in payload["features"]:
                    p = f.get("priority", "P2")
                    color = {"P0": "red", "P1": "yellow", "P2": "green"}.get(p, "white")
                    console.print(f"  [{color}][{p}][/] {f.get('name')}: {f.get('description', '')}")
        return

    if cmd == "pipeline":
        if len(sys.argv) < 3:
            console.print("[red]Usage: python -m agent_system pipeline <task> [--json][/red]")
            return
        task_input = sys.argv[2]
        json_output = "--json" in sys.argv

        from agent_system.agents.ceo_agent import CEOAgent
        from agent_system.core.graph import run_agent_sync

        agent = CEOAgent()
        console.print("[bold]Pipeline[/bold] Product -> Tech -> Test")
        with console.status("[green]Pipeline running..."):
            result = run_agent_sync(agent, task_input)

        if result["status"] == "failed":
            console.print(f"[red]Pipeline failed: {result.get('error')}[/red]")
            sys.exit(1)

        output = result.get("output", {})
        if json_output:
            console.print(json_mod.dumps(output, ensure_ascii=False, indent=2))
        else:
            payload = output.get("payload", {})
            steps = payload.get("steps", [])
            console.print(f"\n[bold]== Pipeline Results ==[/bold]")
            console.print(f"  Status: [{'green' if payload.get('pipeline_status') == 'completed' else 'red'}]{payload.get('pipeline_status')}[/]")
            tree = Tree("Pipeline")
            for step in steps:
                icon = "[green]OK[/]" if step["status"] == "completed" else "[red]FAIL[/]"
                agent_name = step.get("agent", step.get("name", "?"))
                tree.add(f"{agent_name}: {step['status']} {icon}")
            console.print(tree)
        return

    # Default: run as task
    task_input = " ".join(sys.argv[1:])
    from agent_system.agents.product_agent import ProductAgent
    from agent_system.core.graph import run_agent_sync

    agent = ProductAgent()
    console.print(f"Agent: [cyan]product[/cyan]")
    with console.status("[green]Running..."):
        result = run_agent_sync(agent, task_input)
    if result["status"] == "failed":
        console.print(f"[red]Failed: {result.get('error')}[/red]")
        sys.exit(1)
    output = result.get("output", {})
    payload = output.get("payload", {})
    title = payload.get("title", output.get("type", "Output"))
    console.print(f"\n[bold]== {title} ==[/bold]")
    if "features" in payload:
        for f in payload["features"]:
            console.print(f"  [{f.get('priority', 'P2')}] {f.get('name')}: {f.get('description', '')}")


if __name__ == "__main__":
    main()
