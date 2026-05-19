Skill: Security Operations

Purpose

Execute security-sensitive operations safely within defined zones and with full audit trail.

Zone Definitions

Green Zone (default)

Paths: ~/jarvis/, /tmp/jarvis/
Permissions: Full read/write
Confirmation: Not required
Audit: Standard logging
Yellow Zone

Paths: ~/* (excluding Green), /tmp/* (excluding Green)
Permissions: Read always; Write requires user confirmation
Confirmation: Required for writes
Audit: Enhanced logging with diff
Red Zone (requires JARVIS_ZONE=red)

Paths: /etc/, /var/, /system/, /usr/
Permissions: Read-only unless explicitly enabled
Confirmation: Always required
Audit: Full audit trail, git snapshot before any modification
Black Zone (never)

Paths: /proc/*, kernel interfaces
Permissions: Never accessed
Override: Not possible
Lab Mode Network Capabilities

When JARVIS_LAB_MODE=true in an isolated environment:

nmap network scanning
tcpdump/tshark packet capture (requires sudo or cap_net_raw)
Router administration via HTTP (if credentials provided)
Port scanning and service enumeration
Credential Management

Credentials stored in encrypted vault (age encryption)
Vault file: ~/.jarvis/vault/credentials.age
Vault key derived from system entropy, never stored in plaintext
Credentials fetched just-in-time, never held in memory > 60 seconds
Rollback Protocol

Before ANY destructive operation:

git -C ~/jarvis stash push -m "pre-action-$(date +%s)"
Execute action
If action fails: git -C ~/jarvis stash pop
If action succeeds: keep stash for 24h, then clean up
Pentesting (Lab Mode Only)

Used ONLY in isolated, owned lab environments for educational purposes:

Network enumeration: nmap, masscan
Traffic analysis: tcpdump, Wireshark
Web testing: curl, httpie
All results stored in ~/jarvis/lab/findings/
Known Failure Modes

Zone bypass attempt: log and reject with explanation
Vault corruption: restore from backup, never regenerate silently
Audit log full: alert immediately, pause operations until resolved
