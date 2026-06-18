# skills

A collection of Claude Code plugins and agent skills.

## Plugins

| Plugin | Description |
|--------|-------------|
| [`logreader-mcp`](plugins/logreader-mcp) | MCP server for openpilot route analysis: cereal + CAN/DBC, events, anomaly scan, and a panda safety debugger that pins the exact source line blocking each TX. |

## Install (Claude Code plugin marketplace)

```
/plugin marketplace add elkoled/skills
/plugin install logreader-mcp@elkoled-skills
```

See each plugin's README for its own requirements.

## License

[MIT](./LICENSE)
