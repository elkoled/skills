# skills

A collection of Claude Code plugins and agent skills.

## Plugins

| Plugin | Description |
|--------|-------------|
| [`logreader-mcp`](plugins/logreader-mcp) | MCP server for openpilot route analysis: cereal + CAN/DBC, events, anomaly scan, and a panda safety debugger that pins the exact source line blocking each TX. |
| [`mici-ui-mcp`](plugins/mici-ui-mcp) | MCP server that drives the openpilot UI locally: launch it on a private headless display, screenshot it, and inject touch (tap, swipe, long-press) to build and validate UI changes without a device. Optionally replays a route for the onroad UI. |

## Install (Claude Code plugin marketplace)

```
/plugin marketplace add elkoled/skills
/plugin install logreader-mcp@elkoled-skills
/plugin install mici-ui-mcp@elkoled-skills
```

See each plugin's README for its own requirements.

## License

[MIT](./LICENSE)
