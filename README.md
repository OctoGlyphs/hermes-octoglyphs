# OctoGlyphs for Hermes

OctoGlyphs is a privacy-first companion tank for Hermes. It turns session lifecycle, prompt, response, and tool metadata into gems, rewards, danger, and energy in the shared OctoGlyphs tank.

When enabled, Hermes prints a tank link at session start:

```text
Your OctoGlyph is blindly feeding on this Hermes session.
Open your tank: http://localhost:18792/octoglyphs
```

## Privacy promise

The Hermes plugin is passive and metadata-only.

It never sends raw prompts, assistant responses, file contents, tool arguments, terminal output, diffs, or secrets. It emits only sanitized event metadata such as event type, timestamp, prompt length, estimated token count, tool category, success flag, and duration when available.

The plugin does not inject context into Hermes, even though Hermes allows `pre_llm_call` plugins to do so.

## New-user install

Hermes support is a Python plugin, not an npm package. Do not run `npm install` in this folder.

A normal Hermes plugin install clones a Git repository into `~/.hermes/plugins/`. Hermes expects the installed repository root to contain the plugin files, especially `plugin.yaml` and `__init__.py`.

The intended new-user install is:

```bash
hermes plugins install OctoGlyphs/hermes-octoglyphs
hermes plugins enable octoglyphs
hermes
```

`OctoGlyphs/hermes-octoglyphs` is a generated release mirror of this folder. Keep source changes in the main OctoGlyphs monorepo, then publish this folder to the Hermes mirror when releasing. That gives Hermes users the standard install flow without creating a second hand-maintained codebase.

Please file issues and pull requests in the main repository:

```text
https://github.com/OctoGlyphs/OctoGlyphs
```

## Local install for testing

From this repository:

```bash
mkdir -p ~/.hermes/plugins
rm -rf ~/.hermes/plugins/octoglyphs
cp -R ~/Desktop/octoglyphs-release/plugin/hosts/hermes ~/.hermes/plugins/octoglyphs
hermes plugins enable octoglyphs
hermes
```

Then open:

```text
http://localhost:18792/octoglyphs
```

Inside Hermes, you can also run:

```text
/octoglyphs
```

That prints the tank URL, health URL, sidecar state, and privacy reminder.

## Release mirror design

The current repository is multi-host: OpenClaw, Claude Code, and Hermes live under `plugin/hosts/`. The Hermes installer clones a Git repository and reads `plugin.yaml` from the cloned repository root. It does not currently install a subdirectory from a larger repository.

Use a dedicated Hermes release mirror so the installed repository root is exactly the Hermes plugin root:

```text
plugin.yaml
__init__.py
octoglyphs_sidecar.py
public/
tests/
```

The monorepo remains the source of truth. Publish the mirror with:

```bash
./scripts/publish-hermes-plugin.sh --push
```

Set `HERMES_MIRROR_URL` or `HERMES_MIRROR_DIR` if you need a different remote or local checkout path.

## Event mapping

Hermes hook mapping:

```text
on_session_start      -> session.started and response.started
pre_llm_call          -> prompt.sent and response.started
post_tool_call        -> tool.used or commit.created
post_llm_call         -> response.completed
on_session_end        -> session.ended
on_session_finalize   -> session.ended
on_session_reset      -> session.started
```

## Verification

Run static checks and unit tests from the plugin folder:

```bash
cd ~/Desktop/octoglyphs-release/plugin/hosts/hermes
python3 -m py_compile __init__.py octoglyphs_sidecar.py
python3 -m unittest discover -s tests
```

Manual test:

```bash
mkdir -p ~/.hermes/plugins
rm -rf ~/.hermes/plugins/octoglyphs
cp -R ~/Desktop/octoglyphs-release/plugin/hosts/hermes ~/.hermes/plugins/octoglyphs
hermes plugins enable octoglyphs
hermes
```

Expected checks:

- Hermes shows OctoGlyphs in plugin list.
- Session start prints the tank URL.
- `http://localhost:18792/octoglyphs/health` returns healthy JSON.
- Prompt sends spawn or reconcile gems.
- Tool calls create tool rewards.
- Event payloads contain no raw prompt, response, file content, tool args, terminal output, diffs, or secrets.
