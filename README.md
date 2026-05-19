# cli-agent-mvp

Tiny local CLI coding agent built on the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/).
The SDK handles the agent loop; we just supply a few file tools.

## Tools

- `read_file(path)`
- `write_file(path, content)`
- `list_dir(path=".")`
- `run_shell(command)` — 60s timeout, output truncated to 8000 chars

All paths resolve against `AGENT_WORKDIR` (defaults to the current directory).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then provide your API key either via env var or a `.env` file in the project root:

```bash
export OPENAI_API_KEY=sk-...
# or
echo 'OPENAI_API_KEY=sk-...' > .env
```

## Usage

The `bsagent` launcher script is symlinked into `~/.local/bin`, so you can run it
from any directory:

```bash
cd /any/repo
bsagent          # drops into REPL, operating on cwd
```

In the REPL, just type messages and the agent will respond. History is preserved
across turns within the session. Type `exit` (or Ctrl-D) to quit.

You can also still pass a one-shot prompt:

```bash
bsagent "explain the code in this repo"
```

Or pin the working directory explicitly:

```bash
AGENT_WORKDIR=/path/to/repo bsagent
```

### Installing the launcher (first time)

If the `bsagent` command isn't on your PATH yet:

```bash
ln -sf "$PWD/bsagent" ~/.local/bin/bsagent
```

(or copy it to any other directory on your PATH).

## Notes

- This MVP intentionally has **no permission checks**. The agent can read/write any path
  it can reach and run any shell command. Run only on code you trust.
- Conversation history is kept in memory within a session via `result.to_input_list()`.
