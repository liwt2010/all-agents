"""
Memory/Graph extensions for the CLI
"""

from typing import Optional
from datetime import datetime, timezone

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from agent_system.memory.graph import (
    get_graph,
    MultiLinkGraph,
    NodeType,
    LinkType,
    reset_graph,
)
from agent_system.memory.persistence import save_graph, load_graph

console = Console()
graph_app = typer.Typer(help="MultiLinkGraph commands")


@graph_app.command()
def stats(
    detailed: bool = typer.Option(False, "--detailed", "-d", help="Show age distribution per node type"),
):
    """Show graph statistics"""
    graph = get_graph()
    s = graph.stats()

    console.print("[bold]Graph Statistics[/bold]")
    console.print(f"  Total nodes: {s['total_nodes']}")
    console.print(f"  Total links:  {s['total_links']}")

    console.print("\n[yellow]Nodes by type:[/yellow]")
    for ntype, count in sorted(s['nodes_by_type'].items()):
        console.print(f"  {ntype}: {count}")

    console.print("\n[yellow]Links by type:[/yellow]")
    for ltype, count in sorted(s['links_by_type'].items()):
        console.print(f"  {ltype}: {count}")

    if detailed:
        from agent_system.memory.persistence import list_archived
        archived_count = len(list_archived())
        console.print(f"\n[cyan]Archived files:[/cyan] {archived_count}")

        buckets = graph.age_buckets()
        console.print("\n[cyan]Age distribution per node type:[/cyan]")
        table = Table()
        table.add_column("Type", style="yellow")
        for bucket in ["<1d", "1-7d", "7-30d", "30-90d", ">90d"]:
            table.add_column(bucket, justify="right")
        for ntype, counts in buckets.items():
            row = [ntype] + [str(counts.get(b, 0)) for b in ["<1d", "1-7d", "7-30d", "30-90d", ">90d"]]
            table.add_row(*row)
        console.print(table)


@graph_app.command()
def get(
    node_id: str = typer.Argument(..., help="Node ID"),
):
    """Get a node and its neighbors"""
    graph = get_graph()
    ctx = graph.related_with_context(node_id)

    if not ctx["node"]:
        console.print(f"[red]Node not found: {node_id}[/red]")
        return

    node = ctx["node"]
    console.print(f"[bold]Node:[/bold] {node.id}")
    console.print(f"  Type: {node.type.value}")
    console.print(f"  Created: {node.created_at}")
    console.print(f"  Content: {node.content}")
    console.print(f"  Outgoing: {ctx['outgoing_count']} links")
    console.print(f"  Incoming:  {ctx['incoming_count']} links")

    if ctx["neighbors"]:
        console.print(f"\n[cyan]Neighbors:[/cyan]")
        for n in ctx["neighbors"]:
            console.print(f"  [{n.depth}] {n.node.id} ({n.node.type.value}) via {n.link.link_type.value}")

    if ctx["path_to_experience"]:
        console.print(f"\n[green]Related experiences:[/green]")
        for p in ctx["path_to_experience"]:
            console.print(f"  (length {p.length})")


@graph_app.command()
def find(
    node_type: str = typer.Argument(..., help="Node type (task/output/failure/...)"),
    limit: int = typer.Option(10, "--limit", "-l", help="Max results"),
):
    """Find nodes by type"""
    try:
        ntype = NodeType(node_type)
    except ValueError:
        console.print(f"[red]Invalid node type: {node_type}[/red]")
        console.print(f"Valid types: {[t.value for t in NodeType]}")
        return

    graph = get_graph()
    nodes = graph.find_nodes(node_type=ntype)

    if not nodes:
        console.print(f"No nodes of type {node_type}")
        return

    console.print(f"Found {len(nodes)} nodes of type {node_type}:")
    for node in nodes[:limit]:
        status = node.content.get("status", "")
        summary = str(node.content)[:80]
        console.print(f"  {node.id} [{status}] {summary}")

    if len(nodes) > limit:
        console.print(f"  ... and {len(nodes) - limit} more")


@graph_app.command()
def path(
    source: str = typer.Argument(..., help="Source node ID"),
    target: str = typer.Argument(..., help="Target node ID"),
):
    """Find shortest path between two nodes"""
    graph = get_graph()
    result = graph.path(source, target)

    if result.found:
        console.print(f"[green]Path found (length {result.length}):[/green]")
        for node, link in result.path:
            console.print(f"  {node.id} --[{link.link_type.value}]-->")
    else:
        console.print(f"[red]No path found between {source} and {target}[/red]")


@graph_app.command()
def save(
    path: str | None = typer.Option(None, "--path", "-p", help="Save directory"),
):
    """Save graph to disk"""
    from pathlib import Path
    base = Path(path) if path else None
    graph = get_graph()
    count = save_graph(graph, base)
    console.print(f"[green]Saved {count} items[/green]")


@graph_app.command()
def load(
    path: str | None = typer.Option(None, "--path", "-p", help="Load directory"),
):
    """Load graph from disk"""
    from pathlib import Path
    base = Path(path) if path else None
    graph = load_graph(base)
    s = graph.stats()
    console.print(f"[green]Loaded {s['total_nodes']} nodes, {s['total_links']} links[/green]")


@graph_app.command()
def clear():
    """Clear the in-memory graph"""
    reset_graph()
    console.print("[yellow]Graph cleared[/yellow]")


@graph_app.command()
def failures(
    limit: int = typer.Option(5, "--limit", "-l", help="Max results"),
):
    """Show recent failures"""
    graph = get_graph()
    nodes = graph.find_nodes(node_type=NodeType.FAILURE)
    nodes.sort(key=lambda n: n.created_at, reverse=True)

    if not nodes:
        console.print("No failures recorded")
        return

    console.print(f"[red]Recent failures:[/red]")
    for node in nodes[:limit]:
        error = node.content.get("error", "unknown")[:100]
        agent = node.content.get("agent", "?")
        console.print(f"  {node.id} [{agent}] {error}")


# ── Admin commands ──

@graph_app.command()
def cleanup(
    older_than: int = typer.Option(90, "--older-than", help="Archive nodes older than N days"),
    reference_window: int = typer.Option(30, "--reference-window", help="A node with no links in this window is considered an orphan"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Archive orphan old nodes (no links, older than threshold)"""
    graph = get_graph()
    orphans = graph.find_orphan_nodes(reference_window_days=reference_window)
    if not orphans:
        console.print("[green]No orphan nodes found. Nothing to clean up.[/green]")
        return

    # Show preview
    console.print(f"Found {len(orphans)} orphan nodes (no links within {reference_window} days):")
    for n in orphans[:10]:
        age = (datetime.now(timezone.utc) - n.created_at).days if n.created_at else "?"
        console.print(f"  {n.id} ({n.type.value}, age={age}d)")
    if len(orphans) > 10:
        console.print(f"  ... and {len(orphans) - 10} more")

    if not yes:
        confirm = typer.confirm("Archive these nodes?")
        if not confirm:
            console.print("[yellow]Cancelled.[/yellow]")
            return

    count = graph.compact(older_than_days=older_than, reference_window_days=reference_window)
    console.print(f"[green]Archived {count} nodes.[/green]")


@graph_app.command()
def orphans(
    reference_window: int = typer.Option(30, "--reference-window", help="Window in days"),
):
    """Show orphan nodes (no recent links)"""
    graph = get_graph()
    nodes = graph.find_orphan_nodes(reference_window_days=reference_window)
    if not nodes:
        console.print("[green]No orphan nodes.[/green]")
        return

    table = Table(title=f"Orphan Nodes (no links in {reference_window} days)")
    table.add_column("ID", style="cyan")
    table.add_column("Type", style="yellow")
    table.add_column("Age (days)", justify="right")
    for n in nodes:
        age = (datetime.now(timezone.utc) - n.created_at).days if n.created_at else "?"
        table.add_row(n.id, n.type.value, str(age))
    console.print(table)


@graph_app.command()
def vacuum(
    retention_days: int = typer.Option(365, "--retention-days", help="Permanently delete archived files older than N days"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Permanently delete archived nodes past retention period"""
    from agent_system.memory.persistence import list_archived, vacuum_archived
    archived = list_archived()
    if not archived:
        console.print("[green]No archived nodes to vacuum.[/green]")
        return

    console.print(f"Found {len(archived)} archived node files.")
    if not yes:
        confirm = typer.confirm(
            f"Delete archived files older than {retention_days} days?"
        )
        if not confirm:
            console.print("[yellow]Cancelled.[/yellow]")
            return

    count = vacuum_archived(retention_days=retention_days)
    console.print(f"[green]Vacuumed {count} archived files.[/green]")


# Patch stats command to support --detailed
# (Override the original stats defined above)
import click  # noqa: E402

# Re-register the stats command with a --detailed flag
# Keep the original function but add the flag
