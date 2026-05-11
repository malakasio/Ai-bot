Skill: Tool Usage & MCP Servers

Purpose

Use the right tool for each task efficiently and safely.

Available Tools

Core Tools

Tool	When to use	Timeout
bash	System commands, file operations	30s (adjustable)
file_read	Read any file	5s
file_write	Write to Green/Yellow zone	5s
web_search	Real-time information lookup	10s
code_exec	Run Python/JS code in sandbox	60s
MCP Servers

Server	Capabilities	Security Zone
filesystem	Read/write files with zone enforcement	Green/Yellow
network	nmap, tcpdump, curl (lab mode)	Yellow/Red
browser	Playwright automation, DOM interaction	Yellow
system	systemd, process management, crontab	Yellow/Red
github	PR creation, issue management, code review	Yellow
database	Direct PostgreSQL queries	Green
Tool Selection Logic

if task involves files → use filesystem MCP (zone-aware)
if task involves network (lab mode) → use network MCP
if task involves GUI → use browser MCP with Playwright
if task involves system config → use system MCP (requires Yellow+)
if task is a quick shell command → use bash tool
if task needs real-time data → use web_search
Anti-Patterns to Avoid

Never use bash for file operations that span zones (use filesystem MCP)
Never store tool outputs with credentials in working memory > 60s
Never chain more than 5 tools without a checkpoint
Never use browser MCP on banking/financial sites
Known Failure Modes

MCP server crash: restart automatically, log incident
Tool timeout: escalate to larger model with extended timeout
Unexpected output format: parse defensively, never assume schema
