# Heliox OS Daemon

Python backend for the Heliox OS AI System Control Agent. It provides the
append-only experience ledger, durable task loop, temporal context, companion
coordination, hybrid world model, verified online learning, strategy/evolution
harnesses, 21-specialist mesh, Planner/Executor/Verifier, security layers, and
the 156-action system interface.

Typed and spoken requests enter the same observable interaction state machine.
Interactive browser and desktop goals use a bounded observe, act, and verify
loop with fresh screen evidence, target-window re-acquisition, and explicit
no-progress limits. Native application launch is fail-closed and platform
specific: Windows resolves installed-app records and shortcuts, macOS uses
Launch Services, and Linux accepts verified executables or desktop entries.
Issuing a launch command is never treated as proof that the user's goal is
complete.

See the [main README](../README.md) and
[Architecture](../docs/ARCHITECTURE.md) for the full runtime contract.

## SSH Agent (Remote Host Execution)

Heliox includes an optional `SshAgent` for `ssh_command` and `ssh_script`
actions against preconfigured, allowlisted hosts through Paramiko.

### Install

```bash
pip install "pilot-daemon[ssh]"
```

### Configure allowed hosts

Enable SSH and define aliases in `config.toml`:

```toml
[ssh]
enabled = true
connect_timeout_seconds = 10
allowed_hosts = [
  { name = "prod-1", hostname = "10.0.0.10", port = 22, username = "ubuntu", private_key_provider = "ssh_prod_1_key", strict_host_key_checking = true },
]
```

### Store SSH keys

Store the private-key PEM through the authenticated `store_api_key` RPC with
`provider=<private_key_provider>`. The daemon persists it in the operating
system credential store: Windows Credential Manager, macOS Keychain, or a
Secret-Service-compatible Linux keyring.

Keys and passphrases are never logged and are retrieved only when needed. SSH
actions retain the dedicated `ssh_agent` gateway profile, exact host allowlist,
confirmation, irreversibility, audit, and verification behavior.
