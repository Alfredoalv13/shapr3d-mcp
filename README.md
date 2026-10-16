# Shapr3D MCP Server

An MCP server that lets AI assistants (Claude, etc.) model real CAD parts and
work with [Shapr3D](https://www.shapr3d.com/).

## How it works

Shapr3D has **no public API or scripting interface** (it's a
[long-standing community request](https://discourse.shapr3d.com/t/mcp-api/37502)).
So this server takes the only robust route:

1. **Real CAD kernel inside the server.** Models are built programmatically
   with [build123d](https://build123d.readthedocs.io/) on the OpenCascade
   B-rep kernel — the same class of geometry Shapr3D uses (Parasolid-like
   solids, not meshes).
2. **STEP file exchange.** Generated models export as STEP, which Shapr3D
   imports as fully **editable solid bodies**. Files you export *from*
   Shapr3D (STEP/IGES/STL) can be inspected, modified, and converted.
3. **App bridge (macOS and Windows).** Tools to launch Shapr3D, open files
   in it, check its state, and screenshot its window for visual feedback.

```
AI assistant ──MCP──▶ this server ──build123d/OCCT──▶ model.step ──▶ Shapr3D
                          ▲                                            │
                          └────────── STEP/STL export from app ◀───────┘
```

## Tools

The macOS UI tools need Screen Recording and Accessibility permission for the
app hosting the server; Shapr3D draws its own dialogs, so they are found by
OCR, and it ignores input while in the background, so these tools bring it
forward briefly. Run `check_permissions` once at the start.

| Tool | Purpose |
|---|---|
| `create_model` | Build a model from a build123d script (optionally fed by a shared `params_file`), export STEP/STL/3MF/glTF/GLB/BREP |
| `modify_model` | Import an existing file (e.g. Shapr3D STEP export), edit it in code, re-export |
| `run_model_file` | Build from a script file (versionable), with `load_cad` (cached vendor CAD), `include`, `export_part`, checked `cut`/`fuse` |
| `detect_clash` | Pairwise interference check; names assembly parts, separates `expected` overlaps (threads, hoses), reports `near_miss_mm` gaps |
| `query_features` | Cylinders (hole/boss, radius, axis, length) and planes of a part or region - read real dimensions off vendor CAD |
| `section_view` | 2-D cross-section PNG on XY/XZ/YZ, parts coloured, areas listed |
| `quick_look` | One progress sheet: coloured iso + front render and a section, for showing the user as you go |
| `diff_models` | What changed between two versions (e.g. after the user edited in Shapr3D): added/removed regions, new/removed faces |
| `inspect_model` | Solids, bounding box, volume, face/edge counts; per-part inventory for labelled assemblies |
| `convert_model` | Read STEP / IGES / STL / BREP / 3MF, write STEP / STL / 3MF / glTF / GLB / BREP |
| `render_preview` | Shaded PNG preview (iso / standard views / custom angle, optional edge overlay) without opening any app |
| `list_models` | List files in the models workspace |
| `open_in_shapr3d` | Open a file in Shapr3D (STEP imports as editable bodies) |
| `activate_shapr3d` / `shapr3d_status` | Launch / check the app |
| `screenshot_shapr3d` | Capture the Shapr3D window |
| `read_shapr3d_ui` | macOS: OCR the window - visible labels with their positions |
| `click_shapr3d_text` / `click_shapr3d_at` | macOS: click a label found by OCR, or a point from a screenshot |
| `shapr3d_menu` | macOS: click or list menu-bar items, e.g. `["File", "Export to", "STEP…"]` |
| `shapr3d_keys` | macOS: send a shortcut (`cmd+z`, `escape`) or type text |
| `export_from_shapr3d` | macOS: File > Export to > STEP/STL/... and fill the Save panel; focus returns to the previous app |
| `check_permissions` | macOS: warm-up - request Accessibility / Screen Recording / Automation once, report status |
| `build123d_guide` | Modeling syntax cheat sheet for the AI |

## Setup

Requires macOS or Windows, [uv](https://docs.astral.sh/uv/), and Shapr3D
installed (on Windows, the Microsoft Store version).

```sh
uv sync
```

### Claude Code

A project-scoped [.mcp.json](.mcp.json) is included — opening this folder in
Claude Code picks it up automatically. To register it globally:

```sh
claude mcp add shapr3d -s user -- uv run --directory "<path-to-this-folder>" shapr3d-mcp
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "shapr3d": {
      "command": "uv",
      "args": ["run", "--directory", "<path-to-this-folder>", "shapr3d-mcp"]
    }
  }
}
```

## Typical workflow

1. Ask the AI to model something ("design a wall bracket for a 2-inch pipe,
   4 mounting holes for M5 bolts").
2. It writes a build123d script via `create_model`, checks it with
   `render_preview`, and opens the STEP in Shapr3D via `open_in_shapr3d`.
3. You confirm the Import Preferences dialog in Shapr3D (Quality is fine;
   STEP carries mm units) and edit freely — the bodies are native,
   editable solids.
4. To AI-edit an existing design: export STEP from Shapr3D, then ask the AI
   to `inspect_model` / `modify_model` it.

Generated files live in `models/` (override with `SHAPR3D_MCP_WORKDIR`).

### Windows notes

Shapr3D for Windows is a Store app, which only accepts `.shapr` files from
other programs. For STEP and the other formats, `open_in_shapr3d`
launches or activates Shapr3D and copies the file's full path to the
clipboard. Choose **Import** in Shapr3D and paste the path into the file
picker, or drag the file onto the Shapr3D window.

`screenshot_shapr3d` captures the Shapr3D window even when other windows
cover it, but not while it is minimized.

The server loads its CAD and plotting libraries at startup (a few seconds)
rather than on first use. On Windows, loading those native libraries
while the MCP stdio transport is reading stdin deadlocks the process.

## Development

```sh
uv run pytest
```

The suite exercises the modeling tools directly (no MCP transport, no
Shapr3D app needed) in a throwaway workspace.

## Limitations

- `.shapr` files are a proprietary, undocumented format — they can't be read
  or written directly. STEP is the interchange format.
- Parametric history doesn't survive the round-trip (a STEP file is the
  final solid, not the feature tree). Shapr3D's direct-modeling tools work
  on imported solids regardless.
- In-app actions (sketching, export) happen via the user or OS-level
  computer-use automation; there is no app API to drive.
- On Windows, non-`.shapr` files can't be pushed into Shapr3D
  programmatically; the user finishes the import (see Windows notes).

## License

[MIT](LICENSE)
