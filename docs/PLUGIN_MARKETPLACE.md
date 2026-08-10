# Heliox Plugin Marketplace

Heliox publishes plugins through reviewed pull requests to the public
[`VyomKulshrestha/Heliox-OS`](https://github.com/VyomKulshrestha/Heliox-OS)
repository. The app reads the approved catalog from `main`, so merging a plugin
pull request publishes it to the marketplace. A new desktop release is not
required.

## Publishing flow

1. In Heliox, open **Plugin Marketplace** and select **Create Local Plugin**.
2. Test every exposed tool on your device.
3. Fork the Heliox repository and create a branch.
4. Add these files:

   ```text
   plugins/<plugin-name>/
   ├── manifest.json
   └── plugin.py
   ```

5. Add the plugin metadata and package files to `plugins/registry.json`.
6. From the repository root, update the package hashes:

   ```powershell
   python scripts/validate_marketplace.py --write
   ```

7. Verify the finished catalog:

   ```powershell
   python scripts/validate_marketplace.py
   ```

8. Open a pull request explaining the plugin's purpose, external services,
   credentials, side effects, and how reviewers can test each tool.

After CI passes and a maintainer approves and merges the pull request, the
plugin appears when users press **Refresh** in the marketplace. Until then, a
plugin created in the app is local to its creator's device.

## Package contract

- Plugin names use lowercase letters, numbers, and single hyphens, are at most
  64 characters, and cannot begin or end with a hyphen.
- The package path is exactly `plugins/<plugin-name>`.
- Every manifest must set `runtime_type` to `python` or `wasm` and include the
  full `capabilities` object. Missing or unknown capability fields fail closed.
- Python marketplace plugins use `plugin.py` as the entry point and export:

  ```python
  def handle_tool(tool_name, params):
      return {"status": "success"}
  ```

- Python packages require `manifest.json` and `plugin.py`. WASM packages
  require `manifest.json`, a safe relative `wasm_module`, and the declared
  module file. A package may contain at most 64 files and each file may be at
  most 5 MB.
- Every package file has a SHA-256 digest in `plugins/registry.json`. Python
  and JSON digests use canonical LF newlines so Windows and Unix checkouts
  verify identically.
- Tool names are globally unique and use lowercase `snake_case`.
- Dependencies must be declared truthfully. Credentials must come from the
  environment or an authenticated service and must never be committed.
- Fake success responses, fabricated live data, embedded secrets, silent
  telemetry, and undeclared side effects are grounds for rejection.

The required capability contract is:

```json
{
  "filesystem": {"read": [], "write": []},
  "network_domains": [],
  "processes": [],
  "credentials": [],
  "clipboard": {"read": false, "write": false},
  "media": {"camera": false, "microphone": false},
  "data_retention": {"mode": "none", "max_days": 0},
  "destructive_actions": false
}
```

Use exact paths, domains, process names, and credential names. Wildcards,
traversal, undeclared devices, unsafe entry points, duplicate tool ownership,
and catalog/installed-manifest capability mismatches are rejected.

Python packages intentionally permit only reviewed standard-library imports
from `__future__`, `json`, `os`, `typing`, and `urllib`. The validator rejects
`compile`, `eval`, `exec`, and `__import__`. Expand this policy only through a
reviewed platform change, not inside an individual plugin pull request.

## Review and trust model

The public GitHub repository and its reviewed `main` branch are the publication
authority. Marketplace CI validates the catalog, package paths, manifests,
allowed imports, and hashes. Maintainers must still review behavior and run the
plugin because static checks cannot prove that network calls or device actions
are harmless.

Repository administrators should configure the `main` branch ruleset to require:

- the **Validate catalog and packages** status check;
- at least one approving review;
- review from the marketplace `CODEOWNERS`; and
- dismissal of approvals when new commits are pushed.

During installation, Heliox:

1. downloads the catalog over HTTPS from the approved GitHub repository;
2. accepts only a plugin listed in that catalog;
3. downloads only the catalog-declared files;
4. blocks path traversal and oversized files;
5. verifies every SHA-256 digest before installation; and
6. signs the verified local installation for later tamper detection.

If GitHub is unavailable, the app clearly shows a bundled offline catalog.
Bundled entries remain installable, but newly merged plugins appear only after
the GitHub catalog is reachable.

## Runtime isolation and approval

Installation approval does not grant unrestricted daemon access:

- Native Python tools run in a one-shot child broker with a scrubbed
  environment, a 30-second timeout, and only the declared credentials.
  Filesystem, network, process, clipboard, camera, and microphone access are
  default-denied outside the manifest.
- WASM tools run through Wasmtime with path-scoped read/read-write WASI
  preopens, explicit environment grants, a 128 MB default memory limit, epoch
  timeout, and no networking. A WASM package that requests network domains is
  rejected and must use a reviewed native broker instead.
- `plugin_call` and `wasm_call` remain separate planner actions and execution
  paths. The specialist mesh may display the tool as a guarded external
  provider but never imports or executes plugin code itself.
- A plugin declaring `destructive_actions: true` cannot use direct Marketplace
  execution. It must enter the normal planner, world-model, gateway,
  confirmation, durable-claim, audit, and verification path.

For non-destructive tools, clicking **Execute** is an explicit manual request,
but the capability broker remains authoritative. The UI shows the exact grants
before installation and execution.

## Updating a plugin

Submit another pull request that changes the package version and files, then run
the validator with `--write` to refresh hashes. The current MVP requires users
to uninstall the older version and install the updated version after the pull
request is merged.
