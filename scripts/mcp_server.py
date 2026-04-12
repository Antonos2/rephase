#!/usr/bin/env python3
"""
Rephase MCP Server — bridge di coordinamento tra Claude Chat, Claude Code, Cowork e Flow.

Implementa il protocollo MCP (JSON-RPC su stdio) senza dipendenze esterne.
Funziona con qualsiasi Python >= 3.9.

Permette a Claude Chat di:
  - Leggere/aggiornare lo stato del progetto (CLAUDE.md)
  - Gestire la lista task (TASKS.md)
  - Inviare broadcast a tutti gli agenti

Transport: stdio (standard per Claude Desktop / claude.ai MCP)

Configurazione Claude Desktop (~/Library/Application Support/Claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "rephase": {
        "command": "python3",
        "args": ["/Users/smartsmart/Desktop/rephase/scripts/mcp_server.py"]
      }
    }
  }
"""
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
TASKS_MD = REPO_ROOT / "TASKS.md"
BROADCAST_LOG = REPO_ROOT / ".claude" / "broadcast.log"

TOOLS = [
    {
        "name": "get_status",
        "description": "Legge lo stato completo del progetto Rephase da CLAUDE.md",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "update_status",
        "description": "Aggiorna una sezione di CLAUDE.md. Passa section (nome sezione) e content (nuovo contenuto).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "Nome della sezione (es. 'Deploy', 'DNS')"},
                "content": {"type": "string", "description": "Nuovo contenuto della sezione (markdown)"},
            },
            "required": ["section", "content"],
        },
    },
    {
        "name": "get_tasks",
        "description": "Legge la lista task corrente da TASKS.md",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "add_task",
        "description": "Aggiunge un task a TASKS.md",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Descrizione del task"},
                "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"], "description": "Priorità"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "complete_task",
        "description": "Segna un task come completato in TASKS.md (cerca per testo parziale)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Testo parziale del task da completare"},
            },
            "required": ["search"],
        },
    },
    {
        "name": "broadcast",
        "description": "Invia un messaggio broadcast a tutti gli agenti (scritto in .claude/broadcast.log)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Messaggio da inviare"},
            },
            "required": ["message"],
        },
    },
]


# ── Tool implementations ─────────────────────────────────────────────────────

def _get_status(_args):
    if CLAUDE_MD.exists():
        return CLAUDE_MD.read_text(encoding="utf-8")
    return "CLAUDE.md non trovato."


def _update_status(args):
    section = args["section"]
    content = args["content"]
    if not CLAUDE_MD.exists():
        return "CLAUDE.md non trovato."

    text = CLAUDE_MD.read_text(encoding="utf-8")
    header = f"## {section}"

    if header in text:
        start = text.index(header)
        rest = text[start + len(header):]
        next_section = rest.find("\n## ")
        if next_section == -1:
            text = text[:start] + f"{header}\n\n{content}\n"
        else:
            text = text[:start] + f"{header}\n\n{content}\n" + rest[next_section:]
    else:
        text = text.rstrip() + f"\n\n{header}\n\n{content}\n"

    CLAUDE_MD.write_text(text, encoding="utf-8")
    return f"Sezione '{section}' aggiornata in CLAUDE.md."


def _get_tasks(_args):
    if TASKS_MD.exists():
        return TASKS_MD.read_text(encoding="utf-8")
    return "TASKS.md non trovato."


def _add_task(args):
    task = args["task"]
    priority = args.get("priority", "P2")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"- [ ] **{priority}** {task} *(aggiunto {now})*\n"

    if TASKS_MD.exists():
        text = TASKS_MD.read_text(encoding="utf-8")
    else:
        text = "# Rephase — Task List\n\n"

    text += line
    TASKS_MD.write_text(text, encoding="utf-8")
    return f"Task aggiunto: {priority} {task}"


def _complete_task(args):
    search = args["search"].lower()
    if not TASKS_MD.exists():
        return "TASKS.md non trovato."

    lines = TASKS_MD.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if "- [ ]" in line and search in line.lower():
            lines[i] = line.replace("- [ ]", "- [x]", 1)
            TASKS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return f"Task completato: {lines[i].strip()}"
    return f"Nessun task trovato con '{args['search']}'."


def _broadcast(args):
    message = args["message"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"[{now}] {message}\n"

    BROADCAST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(BROADCAST_LOG, "a", encoding="utf-8") as f:
        f.write(entry)
    return f"Broadcast inviato: {message}"


HANDLERS = {
    "get_status": _get_status,
    "update_status": _update_status,
    "get_tasks": _get_tasks,
    "add_task": _add_task,
    "complete_task": _complete_task,
    "broadcast": _broadcast,
}


# ── JSON-RPC / MCP protocol ─────────────────────────────────────────────────

def _send(msg):
    """Invia un messaggio JSON-RPC su stdout."""
    raw = json.dumps(msg)
    sys.stdout.write(raw + "\n")
    sys.stdout.flush()


def _ok(id_, result):
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _error(id_, code, message):
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def handle(request):
    """Gestisce una singola richiesta JSON-RPC MCP."""
    id_ = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "initialize":
        _ok(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "rephase", "version": "1.0.0"},
        })

    elif method == "notifications/initialized":
        pass  # no response needed for notifications

    elif method == "tools/list":
        _ok(id_, {"tools": TOOLS})

    elif method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        handler = HANDLERS.get(name)
        if handler:
            try:
                result_text = handler(arguments)
                _ok(id_, {"content": [{"type": "text", "text": result_text}]})
            except Exception as e:
                _error(id_, -32000, str(e))
        else:
            _error(id_, -32601, f"Tool '{name}' non trovato.")

    elif method == "ping":
        _ok(id_, {})

    else:
        if id_ is not None:
            _error(id_, -32601, f"Metodo '{method}' non supportato.")


# ── Main loop ────────────────────────────────────────────────────────────────

def main():
    """Loop principale: legge JSON-RPC da stdin, risponde su stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            handle(request)
        except json.JSONDecodeError:
            _error(None, -32700, "Parse error")


if __name__ == "__main__":
    main()
